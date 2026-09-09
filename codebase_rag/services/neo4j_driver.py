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

import types
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..types_defs import BatchParams, BatchWrapper, PropertyValue

if TYPE_CHECKING:  # pragma: no cover - import cost only paid at type-check time
    from neo4j import Driver, Session


class _Column:
    """The single attribute the ingestor reads off `cursor.description`."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class Neo4jCursor:
    """One statement's execution, buffered to look like a DB-API cursor."""

    __slots__ = ("_session", "_rows", "_keys")

    def __init__(self, session: Session) -> None:
        self._session = session
        self._rows: list[tuple[PropertyValue, ...]] = []
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
            parameters: dict[str, Any] = {}
        elif isinstance(params, dict):
            parameters = dict(params)
        else:
            raise TypeError(
                "Neo4j parameters must be a mapping; "
                f"got {type(params).__name__}. Batched writes pass "
                "BatchWrapper({'batch': rows})."
            )
        # The driver types every query entry point as `LiteralString` to
        # discourage string-built Cypher, and `Query()` demands one too,
        # so there is no typed route for a statement assembled at
        # runtime. Ours comes from the dialect and the query builders,
        # never from user input; the parameters below are always bound,
        # never interpolated.
        result = self._session.run(
            query,  # ty: ignore[invalid-argument-type]
            parameters,
        )
        self._keys = list(result.keys())
        # Materialise before the result is invalidated by the next
        # statement on this session; the ingestor reads rows after the
        # cursor has been closed.
        self._rows = [tuple(record.values()) for record in result]

    @property
    def description(self) -> Sequence[_Column] | None:
        return [_Column(k) for k in self._keys] if self._keys else None

    def fetchall(self) -> list[tuple[PropertyValue, ...]]:
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

    def __init__(self, session: Session) -> None:
        self._session = session
        self.autocommit = True

    def cursor(self) -> Neo4jCursor:
        return Neo4jCursor(self._session)

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
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ImportError(
                "The neo4j backend needs the `neo4j` package. "
                "Install it with: pip install 'code-graph-rag[neo4j]'"
            ) from exc

        auth = (username, password) if username and password else None
        self._driver: Driver = GraphDatabase.driver(uri, auth=auth)
        self._database = database

    def connect(self) -> Neo4jConnection:
        return Neo4jConnection(self._driver.session(database=self._database))

    def close(self) -> None:
        self._driver.close()
