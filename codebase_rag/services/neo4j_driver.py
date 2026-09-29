"""A cursor-shaped adapter over the official Neo4j driver.

`MemgraphIngestor` is written against a DB-API-ish surface: a connection
with `.cursor()` and `.close()`, and a cursor with `.execute()`,
`.description`, `.fetchall()` and `.close()` (`types_defs.CursorProtocol`).
The Neo4j driver exposes sessions and `Result`/`Record` objects instead,
with no cursor and no `description`.

Rather than fork the ingestor -- 690 lines of batching, grouping,
parallel-flush and error accounting that have nothing to do with the
engine -- this module makes Neo4j *look* like that surface. The ingestor
then works unchanged on either engine, and the only engine-aware code in
the project stays split between here (transport) and `graph_dialects`
(statement text).

Two behaviours are deliberate and load-bearing:

* Each `Neo4jConnection` owns one session, and the ingestor's parallel
  flush already creates one connection per worker, so sessions are never
  shared across threads -- which the Neo4j driver requires. The
  underlying `Driver` is shared and pools connections, which is the
  supported pattern.
* Results are consumed eagerly inside `execute`. The ingestor closes
  cursors in a `finally` and reads `fetchall()` afterwards, whereas a
  Neo4j `Result` is invalid once its session moves on, so buffering at
  execute time is what makes the two models compatible.
"""

from __future__ import annotations

import importlib
import types
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol

from .. import constants as cs
from .. import exceptions as ex
from ..exceptions import ReadOnlyQueryError
from ..types_defs import BatchParams, BatchWrapper, PropertyValue, ResultValue


# The parts of the driver this module uses, typed here rather than taken
# from `neo4j` (issue #2191). The package types every statement entry point,
# `Query()` included, as `LiteralString` to discourage string-built Cypher,
# so there is no typed route for a statement assembled at runtime, and ty
# reports every call whenever the optional extra is installed. Ours come
# from the dialect and the query builders, never from user input; the
# parameters are always bound, never interpolated. The package is imported
# by name (`importlib`), so the checker sees these protocols in both
# environments, with or without the extra, and needs no ignore directive.
class _Record(Protocol):
    def values(self) -> list[ResultValue]: ...


# One EXPLAIN plan node as the driver returns it: nested dicts and lists of
# scalars, narrowed with isinstance where `explain` reads it.
type _PlanField = (
    str | int | float | bool | None | dict[str, _PlanField] | list[_PlanField]
)


class _Summary(Protocol):
    @property
    def plan(self) -> dict[str, _PlanField] | None: ...


class _Result(Protocol):
    def keys(self) -> Sequence[str]: ...

    def __iter__(self) -> Iterator[_Record]: ...

    def consume(self) -> _Summary: ...


class _Session(Protocol):
    def run(
        self,
        query: str,
        parameters: Mapping[str, PropertyValue] | BatchWrapper | None = None,
    ) -> _Result: ...

    def close(self) -> None: ...


class _Driver(Protocol):
    def session(self, **config: str) -> _Session: ...

    def close(self) -> None: ...


class _Column:
    """The single attribute the ingestor reads off `cursor.description`."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class Neo4jCursor:
    """One statement's execution, buffered to look like a DB-API cursor."""

    __slots__ = ("_session", "_rows", "_keys")

    def __init__(self, session: _Session) -> None:
        self._session = session
        self._rows: list[tuple[ResultValue, ...]] = []
        self._keys: list[str] = []

    def execute(
        self,
        query: str,
        params: dict[str, PropertyValue]
        | Sequence[BatchParams]
        | BatchWrapper
        | None = None,
    ) -> None:
        # `BatchWrapper` is a TypedDict (`{"batch": [...]}`), which is a
        # plain dict at runtime and is exactly the parameter map the
        # `UNWIND $batch AS row` idiom wants. A bare sequence would have
        # no name to bind to, so it is rejected rather than guessed at.
        if params is None:
            parameters: Mapping[str, PropertyValue] | BatchWrapper = {}
        elif isinstance(params, dict):
            parameters = params
        else:
            raise TypeError(
                "Neo4j parameters must be a mapping; "
                f"got {type(params).__name__}. Batched writes pass "
                "BatchWrapper({'batch': rows})."
            )
        # `_Session.run` takes a plain `str`; see the protocols above.
        result = self._session.run(query, parameters)
        self._keys = list(result.keys())
        # Materialise before the result is invalidated by the next
        # statement on this session; the ingestor reads rows after the
        # cursor has been closed.
        self._rows = [tuple(record.values()) for record in result]

    @property
    def description(self) -> Sequence[_Column] | None:
        return [_Column(k) for k in self._keys] if self._keys else None

    def fetchall(self) -> list[tuple[ResultValue, ...]]:
        return self._rows

    def close(self) -> None:
        self._rows = []
        self._keys = []


class Neo4jConnection:
    """A session dressed as a connection.

    `autocommit` is accepted and ignored: every statement runs through
    `Session.run`, which is Neo4j's auto-commit transaction, so the
    ingestor's `conn.autocommit = True` is already the behaviour here.
    Accepting the attribute keeps the ingestor engine-agnostic; silently
    ignoring it is safe only because the semantics already match.
    """

    __slots__ = ("_session", "autocommit")

    def __init__(self, session: _Session) -> None:
        self._session = session
        self.autocommit = True

    def cursor(self) -> Neo4jCursor:
        return Neo4jCursor(self._session)

    def explain(self, query: str) -> list[tuple[str, str]]:
        """Every operator of the query's EXPLAIN plan as (type, Details).

        EXPLAIN plans without executing, so nothing in `query` runs here.
        """
        plan = self._session.run(cs.CYPHER_EXPLAIN_PREFIX + query).consume().plan
        operators: list[tuple[str, str]] = []
        pending: list[dict[str, _PlanField]] = [plan] if plan else []
        while pending:
            node = pending.pop()
            # Absent (or null) means none; any other value must have the right
            # shape, a falsy wrong one included (CodeRabbit, PR #2255).
            args = node.get(cs.NEO4J_PLAN_ARGS)
            args = {} if args is None else args
            children = node.get(cs.NEO4J_PLAN_CHILDREN)
            children = [] if children is None else children
            # The read-only guard checks every operator this returns, so a
            # node it cannot fully walk must refuse the query rather than be
            # skipped: a skipped child is an operator nobody inspected
            # (CodeRabbit, PR #2255).
            if not isinstance(args, dict) or not isinstance(children, list):
                raise ReadOnlyQueryError(
                    ex.READ_ONLY_UNREADABLE_PLAN.format(query=query)
                )
            nodes = [child for child in children if isinstance(child, dict)]
            if len(nodes) != len(children):
                raise ReadOnlyQueryError(
                    ex.READ_ONLY_UNREADABLE_PLAN.format(query=query)
                )
            details = args.get(cs.NEO4J_PLAN_DETAILS)
            operators.append(
                (str(node.get(cs.NEO4J_PLAN_OPERATOR_TYPE, "")), str(details or ""))
            )
            pending.extend(nodes)
        return operators

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Neo4jConnection:
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.close()


class Neo4jDriver:
    """Owns the shared `Driver` and hands out per-thread connections."""

    __slots__ = ("_driver", "_database")

    def __init__(
        self,
        uri: str,
        username: str | None,
        password: str | None,
        database: str,
    ) -> None:
        try:
            neo4j = importlib.import_module(cs.NEO4J_MODULE)
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ImportError(
                "The neo4j backend needs the `neo4j` package. "
                "Install it with: pip install 'code-graph-rag[neo4j]'"
            ) from exc

        auth = (username, password) if username and password else None
        self._driver: _Driver = neo4j.GraphDatabase.driver(uri, auth=auth)
        self._database = database

    def connect(self, read_only: bool = False) -> Neo4jConnection:
        """Open a session; `read_only` makes the server refuse any write.

        A READ access-mode session is enforced by Neo4j itself: a write
        fails with `Neo.ClientError.Statement.AccessMode`, whatever the
        statement text looks like.

        This does NOT prove the server is reachable. The Neo4j driver
        connects lazily: `GraphDatabase.driver()` and `session()` touch
        no socket, and an unreachable server first surfaces as
        `ServiceUnavailable` when a query runs. `mgclient.connect()` by
        contrast fails immediately.

        So a reachability probe that only opens a connection detects a
        dead Memgraph and reports a dead Neo4j as HEALTHY -- silently, and
        in the reassuring direction. Any such check must issue a query;
        `HealthChecker.check_memgraph_connection` runs `RETURN 1` for
        exactly this reason.
        """
        if not read_only:
            return Neo4jConnection(self._driver.session(database=self._database))
        read_access: str = importlib.import_module(cs.NEO4J_MODULE).READ_ACCESS
        return Neo4jConnection(
            self._driver.session(
                database=self._database, default_access_mode=read_access
            )
        )

    def close(self) -> None:
        self._driver.close()
