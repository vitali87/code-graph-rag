from __future__ import annotations

import importlib
import threading
import types
from collections import defaultdict
from collections.abc import Generator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import click
import mgclient
import typer
from loguru import logger

from codebase_rag.config import settings
from codebase_rag.types_defs import ConnectionProtocol, CursorProtocol, ResultValue

from .. import exceptions as ex
from .. import logs as ls
from ..constants import (
    CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES,
    CYPHER_EXPLAIN_PREFIX,
    ERR_SUBSTR_ALREADY_EXISTS,
    ERR_SUBSTR_CONSTRAINT,
    FAILED_REL_ROWS_SHOWN,
    KEY_CREATED,
    KEY_FROM_MISSING,
    KEY_FROM_VAL,
    KEY_NAME,
    KEY_PATH,
    KEY_PROJECT_NAME,
    KEY_PROJECT_NAMES,
    KEY_PROPS,
    KEY_PURGED,
    KEY_TO_MISSING,
    KEY_TO_VAL,
    LEGACY_NODE_CONSTRAINTS,
    MERGE_KEY_PROPS_BY_REL,
    NEO4J_EXCEPTIONS_MODULE,
    NODE_NAME_INDEXES,
    NODE_PATH_INDEXES,
    NODE_UNIQUE_CONSTRAINTS,
    REL_ENDPOINT_JOINER,
    REL_ENDPOINT_SOURCE,
    REL_ENDPOINT_TARGET,
)
from ..cypher_queries import (
    CYPHER_ANY_KEYLESS_STRUCTURE,
    CYPHER_ANY_SHARED_STRUCTURE,
    CYPHER_DELETE_ALL,
    CYPHER_DELETE_PROJECT,
    CYPHER_EXPORT_NODES,
    CYPHER_EXPORT_PROJECT_NODES,
    CYPHER_EXPORT_PROJECT_RELATIONSHIPS,
    CYPHER_EXPORT_RELATIONSHIPS,
    CYPHER_LIST_PROJECTS,
    CYPHER_PURGE_CROSS_PROJECT_STRUCTURE,
    CYPHER_PURGE_KEYLESS_STRUCTURE,
    build_create_node_query,
    build_create_relationship_query,
    build_merge_node_query,
    build_merge_relationship_query,
    build_missing_rel_endpoints_query,
    wrap_with_unwind,
)
from ..graph_dialects import (
    DIALECT_MEMGRAPH,
    DIALECT_NEO4J,
    GraphDialect,
    get_dialect,
)
from ..types_defs import (
    BatchParams,
    BatchWrapper,
    GraphData,
    GraphMetadata,
    NodeBatchRow,
    PropertyDict,
    PropertyParams,
    PropertyValue,
    RelBatchRow,
    ResultRow,
)
from ..utils.path_utils import project_roots_from_rows
from .cypher_guard import (
    check_plan,
    memgraph_plan_operators,
    neo4j_plan_operators,
)
from .resource_cleanup import prune_unanchored_resources

if TYPE_CHECKING:
    from .neo4j_driver import Neo4jDriver

# Raised on purpose to end a command with its own exit code once it has told
# the user why; logging them as a failed write buried that message under a
# traceback (#2414). typer's are listed beside click's because a typer that
# vendors click raises its own copies (#1409). Ctrl+C is not here: it stops
# work that did not choose to stop.
_DELIBERATE_EXITS: tuple[type[BaseException], ...] = (
    SystemExit,
    click.exceptions.Exit,
    click.exceptions.Abort,
    click.ClickException,
    typer.Exit,
    typer.Abort,
)


def _apply_memory_limit(
    query: str, mb: int, dialect: GraphDialect | None = None
) -> str:
    """Bound a read's memory using the configured engine's syntax.

    Kept as a module-level function with a defaulted dialect because it is
    imported and called directly by tests and by callers that have no
    ingestor to hand.
    """
    return (dialect or get_dialect(settings.GRAPH_BACKEND)).apply_memory_limit(
        query, mb
    )


def _group_by_merge_keys(
    rel_type: str, params_list: list[RelBatchRow]
) -> dict[tuple[str, ...], list[RelBatchRow]]:
    # Bucket rows by which of the relationship's merge-key props they carry.
    candidate = MERGE_KEY_PROPS_BY_REL.get(rel_type, ())
    by_keys: defaultdict[tuple[str, ...], list[RelBatchRow]] = defaultdict(list)
    for row in params_list:
        props = row[KEY_PROPS] or {}
        by_keys[tuple(p for p in candidate if p in props)].append(row)
    return by_keys


def _created_count(results: Sequence[ResultRow]) -> int:
    total = 0
    for r in results:
        created = r.get(KEY_CREATED, 0)
        if isinstance(created, int):
            total += created
    return total


# pymgclient 1.6 re-exports its C extension through `import *`, which a type
# checker cannot see into, so the exception types are bound once here.
_MgclientDatabaseError: type[Exception] = mgclient.DatabaseError  # ty: ignore[unresolved-attribute]
_MgclientOperationalError: type[Exception] = mgclient.OperationalError  # ty: ignore[unresolved-attribute]


def is_query_rejection(error: BaseException) -> bool:
    """Whether the engine refused the query itself, not the connection.

    A rejected query (a syntax or type error such as sorting on a list) can
    be fixed by asking for a different query; an unreachable server or a
    failed login or a missing permission cannot, so those must not trigger a
    regeneration. In mgclient, `OperationalError` (connection) subclasses
    `DatabaseError`; in neo4j, `AuthError` and `Forbidden` subclass
    `ClientError` (issue #2361).
    """
    if isinstance(error, _MgclientDatabaseError):
        return not isinstance(error, _MgclientOperationalError)
    try:
        neo4j_exceptions = importlib.import_module(NEO4J_EXCEPTIONS_MODULE)
    except ImportError:
        return False
    return isinstance(error, neo4j_exceptions.ClientError) and not isinstance(
        error, (neo4j_exceptions.AuthError, neo4j_exceptions.Forbidden)
    )


def _missing_endpoints(row: ResultRow) -> str:
    return REL_ENDPOINT_JOINER.join(
        side
        for side, key in (
            (REL_ENDPOINT_SOURCE, KEY_FROM_MISSING),
            (REL_ENDPOINT_TARGET, KEY_TO_MISSING),
        )
        if row.get(key) is True
    )


def _log_failed_relationships(
    pattern: tuple[str, str, str, str, str],
    attempted: int,
    successful: int,
    failed_rows: Sequence[ResultRow] = (),
) -> None:
    # Every relationship type, not only CALLS: a lost edge of any other type
    # showed up only as a count in the INFO flush summary (issue #2400). The
    # rows named are the ones the endpoint lookup found without a node; the
    # head of the batch, listed before, was nearly always rows that had been
    # written (issue #2438).
    failed = attempted - successful
    if failed <= 0:
        return
    from_label, _, rel_type, to_label, _ = pattern
    logger.warning(
        ls.MG_RELS_FAILED.format(
            count=failed,
            attempted=attempted,
            from_label=from_label,
            rel_type=rel_type,
            to_label=to_label,
        )
    )
    named = [
        (row, missing) for row in failed_rows if (missing := _missing_endpoints(row))
    ][:FAILED_REL_ROWS_SHOWN]
    for index, (row, missing) in enumerate(named, start=1):
        logger.warning(
            ls.MG_REL_FAILED_ROW.format(
                index=index,
                from_label=from_label,
                from_val=row.get(KEY_FROM_VAL),
                to_label=to_label,
                to_val=row.get(KEY_TO_VAL),
                missing=missing,
            )
        )
    if named and failed > len(named):
        logger.warning(ls.MG_RELS_FAILED_MORE.format(count=failed - len(named)))


class MemgraphIngestor:
    __slots__ = (
        "_conn_lock",
        "_node_flush_lock",
        "_executor",
        "_host",
        "_port",
        "_username",
        "_password",
        "_use_merge",
        "_dialect",
        "_driver",
        "_driver_lock",
        "_rel_count",
        "_rel_groups",
        "batch_size",
        "conn",
        "node_buffer",
    )

    def __init__(
        self,
        host: str,
        port: int,
        batch_size: int = 1000,
        username: str | None = None,
        password: str | None = None,
        use_merge: bool = True,
        dialect: GraphDialect | None = None,
    ):
        # The engine is resolved once here rather than read from settings at
        # each call site, so a single ingestor cannot straddle two dialects
        # mid-run if configuration changes underneath it.
        self._dialect = dialect or get_dialect(settings.GRAPH_BACKEND)
        self._driver: object | None = None
        self._driver_lock = threading.Lock()
        self._host = host
        self._port = port
        self._username = username.strip() if username and username.strip() else None
        self._password = password.strip() if password and password.strip() else None
        # Only for the engine these credentials belong to: Neo4j
        # authenticates with NEO4J_USERNAME/NEO4J_PASSWORD, so a leftover
        # half-set MEMGRAPH_* pair must not stop a valid Neo4j
        # deployment from starting.
        if self._dialect.name == DIALECT_MEMGRAPH and (
            (self._username is None) != (self._password is None)
        ):
            raise ValueError(ex.AUTH_INCOMPLETE)
        if batch_size < 1:
            raise ValueError(ex.BATCH_SIZE)
        self.batch_size = batch_size
        self._use_merge = use_merge
        self._conn_lock = threading.Lock()
        self._node_flush_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self.conn: ConnectionProtocol | None = None
        self.node_buffer: list[tuple[str, dict[str, PropertyValue]]] = []
        self._rel_count = 0
        self._rel_groups: defaultdict[
            tuple[str, str, str, str, str], list[RelBatchRow]
        ] = defaultdict(list)

    def __enter__(self) -> MemgraphIngestor:
        logger.debug(ls.MG_CONNECTING.format(host=self._host, port=self._port))
        try:
            self.conn = self._create_connection()
        except Exception as e:
            # The driver's error does not say where it tried to connect, and
            # the line that did is DEBUG now (issue #2398).
            logger.error(
                ls.MG_CONNECT_FAILED.format(host=self._host, port=self._port, error=e)
            )
            raise
        self._executor = ThreadPoolExecutor(max_workers=settings.FLUSH_THREAD_POOL_SIZE)
        logger.debug(ls.MG_CONNECTED)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        try:
            if exc_type:
                if issubclass(exc_type, _DELIBERATE_EXITS):
                    # Kept at DEBUG with its traceback: a command may turn a
                    # real failure into its own message and an exit, and the
                    # cause should stay one LOGURU_LEVEL away.
                    logger.opt(exception=exc_val).debug(
                        ls.MG_DELIBERATE_EXIT.format(kind=exc_type.__name__)
                    )
                elif issubclass(exc_type, Exception):
                    logger.exception(ls.MG_EXCEPTION.format(error=exc_val))
                else:
                    # Ctrl+C or a cancelled task: the user stopped the run,
                    # and a traceback here read as a crash.
                    logger.warning(ls.MG_INTERRUPTED)
                # Best-effort flush: persist buffered nodes/relationships even when
                # an exception occurred. Catch broad Exception so a secondary flush
                # failure never masks the original.
                try:
                    self.flush_all()
                except Exception as flush_err:
                    logger.error(ls.MG_FLUSH_ERROR.format(error=flush_err))
            else:
                self.flush_all()
        finally:
            if self._executor:
                self._executor.shutdown(wait=True)
                self._executor = None
            if self.conn:
                self.conn.close()
                logger.debug(ls.MG_DISCONNECTED)
            # Sessions handed to flush workers are closed by those
            # workers; the pooled driver behind them is owned here and
            # would otherwise leak its connection pool for the life of
            # the process.
            self.close_driver()

    async def __aenter__(self) -> MemgraphIngestor:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)

    @contextmanager
    def _get_cursor(self) -> Generator[CursorProtocol, None, None]:
        if not self.conn:
            raise ConnectionError(ex.CONN)
        with self._conn_lock:
            cursor: CursorProtocol | None = None
            try:
                cursor = self.conn.cursor()
                yield cursor
            finally:
                if cursor:
                    cursor.close()

    def _cursor_to_results(self, cursor: CursorProtocol) -> list[ResultRow]:
        if not cursor.description:
            return []
        column_names = [desc.name for desc in cursor.description]
        return [
            dict[str, ResultValue](zip(column_names, row)) for row in cursor.fetchall()
        ]

    def _execute_query(
        self,
        query: str,
        params: dict[str, PropertyValue] | None = None,
    ) -> list[ResultRow]:
        params = params or {}
        with self._get_cursor() as cursor:
            try:
                cursor.execute(query, params)
                return self._cursor_to_results(cursor)
            except Exception as e:
                if (
                    ERR_SUBSTR_ALREADY_EXISTS not in str(e).lower()
                    and ERR_SUBSTR_CONSTRAINT not in str(e).lower()
                ):
                    logger.error(ls.MG_CYPHER_ERROR.format(error=e))
                    logger.error(ls.MG_CYPHER_QUERY.format(query=query))
                    logger.error(ls.MG_CYPHER_PARAMS.format(params=params))
                raise

    def _create_connection(self) -> ConnectionProtocol:
        """Open one connection for the configured engine.

        The parallel flush calls this once per worker, so whatever comes
        back must be safe to use from a single thread and cheap enough to
        create per flush group. For Neo4j that is a session drawn from a
        shared, pooled `Driver`; for Memgraph it is a real socket.
        """
        if self._dialect.name == DIALECT_NEO4J:
            return self._neo4j_driver().connect()
        if self._username is not None:
            conn = mgclient.connect(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
            )
        else:
            conn = mgclient.connect(host=self._host, port=self._port)
        conn.autocommit = True
        return conn

    def close_driver(self) -> None:
        """Release the Neo4j connection pool, if one was ever built.

        Sessions handed to flush workers are closed by those workers; the
        pooled driver behind them is owned by this object and would
        otherwise outlive it. A Memgraph run never builds one, so this is
        a no-op there.
        """
        driver = self._driver
        if driver is not None:
            cast("Neo4jDriver", driver).close()
            self._driver = None

    def _neo4j_driver(self) -> Neo4jDriver:
        """The process-wide Neo4j driver, created on first use.

        One `Driver` pools connections for the whole ingestor; creating one
        per flush group would defeat that pooling and open a new TCP
        connection per label.
        """
        from .neo4j_driver import Neo4jDriver as _Neo4jDriver

        # The parallel flush calls this from several worker threads at
        # once, so an unguarded `if self._driver is None` would build and
        # leak more than one connection pool.
        with self._driver_lock:
            if self._driver is None:
                self._driver = _Neo4jDriver(
                    uri=settings.NEO4J_URI,
                    username=settings.NEO4J_USERNAME,
                    password=settings.NEO4J_PASSWORD,
                    database=settings.NEO4J_DATABASE,
                )
            return cast("Neo4jDriver", self._driver)

    def _execute_batch_on(
        self,
        conn: ConnectionProtocol,
        query: str,
        params_list: Sequence[BatchParams],
    ) -> None:
        if not params_list:
            return
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(wrap_with_unwind(query), BatchWrapper(batch=params_list))
        except Exception as e:
            if ERR_SUBSTR_ALREADY_EXISTS not in str(e).lower():
                logger.error(ls.MG_BATCH_ERROR.format(error=e))
                logger.error(ls.MG_CYPHER_QUERY.format(query=query))
                if len(params_list) > 10:
                    logger.error(
                        ls.MG_BATCH_PARAMS_TRUNCATED.format(
                            count=len(params_list), params=params_list[:10]
                        )
                    )
                else:
                    logger.error(ls.MG_CYPHER_PARAMS.format(params=params_list))
            raise
        finally:
            if cursor:
                cursor.close()

    def _execute_batch_with_return_on(
        self,
        conn: ConnectionProtocol,
        query: str,
        params_list: Sequence[BatchParams],
    ) -> list[ResultRow]:
        if not params_list:
            return []
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(wrap_with_unwind(query), BatchWrapper(batch=params_list))
            return self._cursor_to_results(cursor)
        except Exception as e:
            logger.error(ls.MG_BATCH_ERROR.format(error=e))
            logger.error(ls.MG_CYPHER_QUERY.format(query=query))
            raise
        finally:
            if cursor:
                cursor.close()

    def clean_database(self) -> None:
        logger.info(ls.MG_CLEANING_DB)
        self._execute_query(CYPHER_DELETE_ALL)
        logger.info(ls.MG_DB_CLEANED)

    def list_projects(self) -> list[str]:
        result = self.fetch_all(CYPHER_LIST_PROJECTS)
        return [str(r[KEY_NAME]) for r in result]

    def list_project_roots(self) -> dict[str, str | None]:
        return project_roots_from_rows(self.fetch_all(CYPHER_LIST_PROJECTS))

    def delete_project(self, project_name: str) -> None:
        logger.info(ls.MG_DELETING_PROJECT.format(project_name=project_name))
        self._execute_query(CYPHER_DELETE_PROJECT, {KEY_PROJECT_NAME: project_name})
        # Shared prefix-less nodes (Resources, ExternalModules) only lose
        # their edges above; drop the ones this project alone anchored.
        prune_unanchored_resources(self)
        self._execute_query(CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
        logger.info(ls.MG_PROJECT_DELETED.format(project_name=project_name))

    def ensure_constraints(self) -> None:
        logger.info(ls.MG_ENSURING_CONSTRAINTS)
        self._migrate_legacy_path_keys()
        for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
            try:
                self._execute_query(self._dialect.create_constraint(label, prop))
            except Exception:  # noqa: S110 - _execute_query logged it; DDL failure must not stop ingestion
                pass
        logger.info(ls.MG_CONSTRAINTS_DONE)
        self._ensure_indexes()

    def _migrate_legacy_path_keys(self) -> None:
        """Retire the superseded Folder/File relative-path keys (issue #897).

        A database that still enforces the legacy constraints was written by
        the old key, which merged same-layout projects onto shared nodes; the
        leftover constraint would also reject the second same-relative-path
        node the current scheme creates. Merged nodes cannot be split, so
        they are purged along with keyless legacy rows; re-indexing rebuilds
        them with per-project identity. Dropping a constraint is idempotent
        in Memgraph, so any failure here is real and propagates. The purge
        keys off the data, not the constraints: damage outlives the schema
        when an earlier partial upgrade already dropped them.
        """
        existing_rows = self._execute_query(self._dialect.show_constraints())
        # Carry the server's own name for each match: engines that drop by
        # name (Neo4j) must drop the constraint that actually exists, not
        # one whose name we derived -- a legacy constraint predates this
        # code and may be named anything.
        legacy_present: list[tuple[str, str, str | None]] = []
        for label, prop in LEGACY_NODE_CONSTRAINTS:
            for row in existing_rows:
                if self._dialect.constraint_row_matches(row, label, prop):
                    legacy_present.append(
                        (label, prop, self._dialect.constraint_row_name(row))
                    )
                    break
        for label, prop, discovered_name in legacy_present:
            self._execute_query(
                self._dialect.drop_constraint(label, prop, discovered_name)
            )
        damaged = bool(self._execute_query(CYPHER_ANY_SHARED_STRUCTURE)) or bool(
            self._execute_query(CYPHER_ANY_KEYLESS_STRUCTURE)
        )
        if not damaged:
            return
        purged = 0
        for purge_query in (
            CYPHER_PURGE_CROSS_PROJECT_STRUCTURE,
            CYPHER_PURGE_KEYLESS_STRUCTURE,
        ):
            rows = self._execute_query(purge_query)
            if rows:
                purged += int(str(rows[0][KEY_PURGED]))
        if purged:
            logger.warning(ls.MG_LEGACY_PURGE.format(count=purged))

    def _ensure_indexes(self) -> None:
        logger.info(ls.MG_ENSURING_INDEXES)
        for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
            try:
                self._execute_query(self._dialect.create_index(label, prop))
            except Exception:  # noqa: S110 - _execute_query logged it; DDL failure must not stop ingestion
                pass
        # The unique-key indexes serve MERGE at write time; generated Cypher
        # reads filter on bare `name`, which needs its own label+name index
        # or every lookup is a full label scan.
        for label in NODE_NAME_INDEXES:
            try:
                self._execute_query(self._dialect.create_index(label, KEY_NAME))
            except Exception:  # noqa: S110 - _execute_query logged it; DDL failure must not stop ingestion
                pass
        for label in NODE_PATH_INDEXES:
            try:
                self._execute_query(self._dialect.create_index(label, KEY_PATH))
            except Exception:  # noqa: S110 - _execute_query logged it; DDL failure must not stop ingestion
                pass
        logger.info(ls.MG_INDEXES_DONE)

    def ensure_node_batch(
        self, label: str, properties: dict[str, PropertyValue]
    ) -> None:
        self.node_buffer.append((label, properties))
        if len(self.node_buffer) >= self.batch_size:
            logger.debug(ls.MG_NODE_BUFFER_FLUSH, size=self.batch_size)
            self.flush_nodes()

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: dict[str, PropertyValue] | None = None,
    ) -> None:
        from_label, from_key, from_val = from_spec
        to_label, to_key, to_val = to_spec
        pattern = (from_label, from_key, rel_type, to_label, to_key)
        self._rel_groups[pattern].append(
            RelBatchRow(from_val=from_val, to_val=to_val, props=properties or {})
        )
        self._rel_count += 1
        if self._rel_count >= self.batch_size:
            logger.debug(ls.MG_REL_BUFFER_FLUSH, size=self.batch_size)
            self.flush_nodes()
            self.flush_relationships()

    def _flush_node_label_group(
        self,
        label: str,
        props_list: list[dict[str, PropertyValue]],
        conn: ConnectionProtocol | None = None,
    ) -> tuple[int, int]:
        if not props_list:
            return 0, 0

        id_key = NODE_UNIQUE_CONSTRAINTS.get(label)
        if not id_key:
            logger.warning(ls.MG_NO_CONSTRAINT.format(label=label))
            return 0, len(props_list)

        batch_rows: list[NodeBatchRow] = []
        skipped = 0
        for props in props_list:
            if id_key not in props:
                logger.warning(
                    ls.MG_MISSING_PROP.format(
                        label=label, key=id_key, prop_keys=list(props.keys())
                    )
                )
                skipped += 1
                continue
            row_props: PropertyDict = {k: v for k, v in props.items() if k != id_key}
            batch_rows.append(NodeBatchRow(id=props[id_key], props=row_props))

        if not batch_rows:
            return 0, skipped

        build_query = (
            build_merge_node_query if self._use_merge else build_create_node_query
        )
        query = build_query(label, id_key)
        target_conn = conn or self.conn
        if not target_conn:
            logger.warning(ls.MG_NO_CONN_NODES.format(label=label))
            return 0, skipped + len(batch_rows)
        lock = self._conn_lock if conn is None else nullcontext()
        with lock:
            self._execute_batch_on(target_conn, query, batch_rows)
        return len(batch_rows), skipped

    def _flush_node_group_with_own_conn(
        self,
        label: str,
        props_list: list[dict[str, PropertyValue]],
    ) -> tuple[int, int]:
        conn = self._create_connection()
        try:
            return self._flush_node_label_group(label, props_list, conn=conn)
        finally:
            conn.close()

    def _flush_rel_group_with_own_conn(
        self,
        pattern: tuple[str, str, str, str, str],
        params_list: list[RelBatchRow],
    ) -> tuple[int, int]:
        conn = self._create_connection()
        try:
            return self._flush_rel_pattern_group(pattern, params_list, conn=conn)
        finally:
            conn.close()

    def flush_nodes(self) -> None:
        with self._node_flush_lock:
            self._flush_nodes()

    def _flush_nodes(self) -> None:
        if not self.node_buffer:
            return

        buffer_size = len(self.node_buffer)
        buffered_nodes = self.node_buffer[:buffer_size]
        nodes_by_label: defaultdict[str, list[dict[str, PropertyValue]]] = defaultdict(
            list
        )
        for label, props in buffered_nodes:
            nodes_by_label[label].append(props)

        if self._executor and len(nodes_by_label) > 1:
            outcome = self._flush_node_groups_parallel(self._executor, nodes_by_label)
        else:
            outcome = self._flush_node_groups_serial(nodes_by_label)
        flushed_total, skipped_total, failed_labels, first_error = outcome

        logger.info(
            ls.MG_NODES_FLUSHED.format(flushed=flushed_total, total=buffer_size)
        )
        if skipped_total:
            logger.info(ls.MG_NODES_SKIPPED.format(count=skipped_total))
        self.node_buffer[:buffer_size] = [
            node for node in buffered_nodes if node[0] in failed_labels
        ]

        if first_error is not None:
            raise first_error

    def _flush_node_groups_parallel(
        self,
        executor: ThreadPoolExecutor,
        nodes_by_label: dict[str, list[dict[str, PropertyValue]]],
    ) -> tuple[int, int, set[str], Exception | None]:
        # Each label group on its own connection; a failed label keeps its
        # buffered nodes for a retry, and the first failure is re-raised.
        logger.debug(
            ls.MG_PARALLEL_FLUSH_NODES.format(
                count=len(nodes_by_label),
                workers=settings.FLUSH_THREAD_POOL_SIZE,
            )
        )
        futures = {
            executor.submit(
                self._flush_node_group_with_own_conn, label, props_list
            ): label
            for label, props_list in nodes_by_label.items()
        }
        flushed_total = 0
        skipped_total = 0
        failed_labels: set[str] = set()
        first_error: Exception | None = None
        for future in as_completed(futures):
            label = futures[future]
            try:
                flushed, skipped = future.result()
            except Exception as e:
                failed_labels.add(label)
                logger.error(ls.MG_LABEL_FLUSH_ERROR.format(label=label, error=e))
                if first_error is None:
                    first_error = e
                continue
            flushed_total += flushed
            skipped_total += skipped
        return flushed_total, skipped_total, failed_labels, first_error

    def _flush_node_groups_serial(
        self, nodes_by_label: dict[str, list[dict[str, PropertyValue]]]
    ) -> tuple[int, int, set[str], Exception | None]:
        flushed_total = 0
        skipped_total = 0
        failed_labels: set[str] = set()
        first_error: Exception | None = None
        for label, props_list in nodes_by_label.items():
            try:
                flushed, skipped = self._flush_node_label_group(label, props_list)
            except Exception as e:
                failed_labels.add(label)
                logger.error(ls.MG_LABEL_FLUSH_ERROR.format(label=label, error=e))
                if first_error is None:
                    first_error = e
                continue
            flushed_total += flushed
            skipped_total += skipped
        return flushed_total, skipped_total, failed_labels, first_error

    def _flush_rel_pattern_group(
        self,
        pattern: tuple[str, str, str, str, str],
        params_list: list[RelBatchRow],
        conn: ConnectionProtocol | None = None,
    ) -> tuple[int, int]:
        from_label, from_key, rel_type, to_label, to_key = pattern
        has_props = any(p[KEY_PROPS] for p in params_list)
        if self._use_merge:
            by_keys = _group_by_merge_keys(rel_type, params_list)
            if len(by_keys) > 1:
                # Rows for the same endpoints may carry different distinguishing
                # props (issue #722); flush each merge-key signature on its own so
                # a prop absent from one row is not dropped from the key for the
                # rest, which would re-collapse the parallel provenance edges.
                # Pass `conn` through unchanged to preserve the lock semantics.
                totals = [
                    self._flush_rel_pattern_group(pattern, rows, conn=conn)
                    for rows in by_keys.values()
                ]
                return sum(t for t, _ in totals), sum(s for _, s in totals)
            merge_key_props = next(iter(by_keys), ())
            query = build_merge_relationship_query(
                from_label,
                from_key,
                rel_type,
                to_label,
                to_key,
                has_props,
                merge_key_props=merge_key_props,
            )
        else:
            query = build_create_relationship_query(
                from_label, from_key, rel_type, to_label, to_key, has_props
            )
        return self._execute_rel_pattern_query(pattern, query, params_list, conn)

    def _execute_rel_pattern_query(
        self,
        pattern: tuple[str, str, str, str, str],
        query: str,
        params_list: list[RelBatchRow],
        conn: ConnectionProtocol | None,
    ) -> tuple[int, int]:
        target_conn = conn or self.conn
        if not target_conn:
            logger.warning(ls.MG_NO_CONN_RELS.format(pattern=pattern))
            return len(params_list), 0
        lock = self._conn_lock if conn is None else nullcontext()
        with lock:
            results = self._execute_batch_with_return_on(
                target_conn, query, params_list
            )
            batch_successful = _created_count(results)
            failed_rows = (
                self._missing_endpoint_rows(target_conn, pattern, params_list)
                if batch_successful < len(params_list)
                else []
            )

        _log_failed_relationships(
            pattern, len(params_list), batch_successful, failed_rows
        )

        return len(params_list), batch_successful

    def _missing_endpoint_rows(
        self,
        conn: ConnectionProtocol,
        pattern: tuple[str, str, str, str, str],
        params_list: list[RelBatchRow],
    ) -> list[ResultRow]:
        # Only runs for a batch that lost rows. It is a diagnostic: the write
        # already happened, so a failed lookup must not fail the flush, and
        # the count of lost rows is still reported without it.
        from_label, from_key, rel_type, to_label, to_key = pattern
        query = build_missing_rel_endpoints_query(
            from_label, from_key, to_label, to_key, FAILED_REL_ROWS_SHOWN
        )
        try:
            return self._execute_batch_with_return_on(conn, query, params_list)
        except Exception as e:
            logger.warning(ls.MG_RELS_FAILED_LOOKUP.format(rel_type=rel_type, error=e))
            return []

    def flush_relationships(self) -> None:
        if not self._rel_count:
            return

        if self._executor and len(self._rel_groups) > 1:
            total_attempted, total_successful, first_error = (
                self._flush_rel_groups_parallel(self._executor)
            )
        else:
            total_attempted, total_successful, first_error = (
                self._flush_rel_groups_serial()
            )

        logger.info(
            ls.MG_RELS_FLUSHED.format(
                total=self._rel_count,
                success=total_successful,
                failed=total_attempted - total_successful,
            )
        )
        self._rel_count = 0
        self._rel_groups.clear()

        if first_error is not None:
            raise first_error

    def _flush_rel_groups_parallel(
        self, executor: ThreadPoolExecutor
    ) -> tuple[int, int, Exception | None]:
        # Each pattern group on its own connection; every group still runs
        # when one fails, and the first failure is re-raised by the caller.
        logger.debug(
            ls.MG_PARALLEL_FLUSH_RELS.format(
                count=len(self._rel_groups),
                workers=settings.FLUSH_THREAD_POOL_SIZE,
            )
        )
        futures = {
            executor.submit(
                self._flush_rel_group_with_own_conn, pattern, params_list
            ): pattern
            for pattern, params_list in self._rel_groups.items()
        }
        total_attempted = 0
        total_successful = 0
        first_error: Exception | None = None
        for future in as_completed(futures):
            pattern = futures[future]
            try:
                attempted, successful = future.result()
            except Exception as e:
                logger.error(ls.MG_REL_FLUSH_ERROR.format(pattern=pattern, error=e))
                if first_error is None:
                    first_error = e
                continue
            total_attempted += attempted
            total_successful += successful
        return total_attempted, total_successful, first_error

    def _flush_rel_groups_serial(self) -> tuple[int, int, Exception | None]:
        total_attempted = 0
        total_successful = 0
        first_error: Exception | None = None
        for pattern, params_list in self._rel_groups.items():
            try:
                attempted, successful = self._flush_rel_pattern_group(
                    pattern, params_list
                )
            except Exception as e:
                logger.error(ls.MG_REL_FLUSH_ERROR.format(pattern=pattern, error=e))
                if first_error is None:
                    first_error = e
                continue
            total_attempted += attempted
            total_successful += successful
        return total_attempted, total_successful, first_error

    def flush_all(self) -> None:
        logger.debug(ls.MG_FLUSH_START)
        self.flush_nodes()
        self.flush_relationships()
        logger.debug(ls.MG_FLUSH_COMPLETE)

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        bounded_query = _apply_memory_limit(
            query, settings.QUERY_MEMORY_LIMIT_MB, self._dialect
        )
        logger.debug(ls.MG_FETCH_QUERY, query=bounded_query, params=params)
        return self._execute_query(
            bounded_query, dict(params) if params is not None else None
        )

    def fetch_read_only(self, query: str) -> list[ResultRow]:
        """Run an untrusted (LLM-generated) query so that it cannot write.

        The text checks in `services.llm` are the first layer; this is the
        one that does not depend on reading the query text correctly. On both
        engines the query is planned with EXPLAIN and refused before it runs
        unless every planned operator is a known read and every procedure is
        allowed. Neo4j additionally runs it in a READ access-mode session,
        which its driver documents as a routing hint rather than access
        control, so it is not relied on.
        """
        bounded_query = _apply_memory_limit(
            query, settings.QUERY_MEMORY_LIMIT_MB, self._dialect
        )
        logger.debug(ls.MG_FETCH_QUERY, query=bounded_query, params=None)
        if self._dialect.name == DIALECT_NEO4J:
            with self._neo4j_driver().connect(read_only=True) as conn:
                check_plan(neo4j_plan_operators(conn.explain(bounded_query)), query)
                cursor = conn.cursor()
                cursor.execute(bounded_query)
                return self._cursor_to_results(cursor)
        plan = self._execute_query(CYPHER_EXPLAIN_PREFIX + bounded_query)
        check_plan(
            memgraph_plan_operators(
                [str(next(iter(row.values()), "")) for row in plan]
            ),
            query,
        )
        return self._execute_query(bounded_query)

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        logger.debug(ls.MG_WRITE_QUERY, query=query, params=params)
        self._execute_query(query, dict(params) if params is not None else None)

    def export_graph_to_dict(self, project_names: Sequence[str] = ()) -> GraphData:
        """The whole shared graph, or what `project_names` own when given.

        A scoped file records its projects in the metadata, so a reader can
        tell it from a whole-graph export with the same shape.
        """
        logger.info(ls.MG_EXPORTING)

        if project_names:
            params: PropertyParams = {KEY_PROJECT_NAMES: list(project_names)}
            nodes_data = self.fetch_all(CYPHER_EXPORT_PROJECT_NODES, params)
            relationships_data = self.fetch_all(
                CYPHER_EXPORT_PROJECT_RELATIONSHIPS, params
            )
        else:
            nodes_data = self.fetch_all(CYPHER_EXPORT_NODES)
            relationships_data = self.fetch_all(CYPHER_EXPORT_RELATIONSHIPS)

        metadata = GraphMetadata(
            total_nodes=len(nodes_data),
            total_relationships=len(relationships_data),
            exported_at=self._get_current_timestamp(),
        )
        if project_names:
            metadata["projects"] = list(project_names)

        logger.info(
            ls.MG_EXPORTED.format(nodes=len(nodes_data), rels=len(relationships_data))
        )
        return GraphData(
            nodes=nodes_data,
            relationships=relationships_data,
            metadata=metadata,
        )

    def _get_current_timestamp(self) -> str:
        return datetime.now(UTC).isoformat()
