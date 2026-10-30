"""The graph ingestor: one object that writes and reads the graph for the
engine GRAPH_BACKEND selects.

`GraphIngestor` owns the connection's lifecycle and composes the rest: the
session that connects and runs a statement (graph_session.py), the write
batching (graph_batching.py) and the query helpers (graph_queries.py).
"""

from __future__ import annotations

import threading
import types
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache

import typer
from loguru import logger

from codebase_rag.config import settings

from .. import exceptions as ex
from .. import logs as ls
from ..graph_dialects import (
    DIALECT_MEMGRAPH,
    DIALECT_SQLITE,
    GraphDialect,
    get_dialect,
)
from .graph_batching import GraphBatching
from .graph_queries import GraphQueries
from .graph_session import GraphUnavailableError, is_query_rejection

__all__ = [
    "GraphConnection",
    "GraphIngestor",
    "GraphUnavailableError",
    "WriteTuning",
    "is_query_rejection",
]

# Raised on purpose to end a command with its own exit code once it has told
# the user why; logging them as a failed write buried that message under a
# traceback (#2414). A usage error is one too: typer prints it and exits 2.
# Ctrl+C is not here: it stops work that did not choose to stop.
_DELIBERATE_EXITS: tuple[type[BaseException], ...] = (
    SystemExit,
    typer.Exit,
    typer.Abort,
    typer.BadParameter,
)


@cache
def _note_memgraph_set_without_backend() -> None:
    # Once per process: a server opens an ingestor per request, and the
    # setting cannot change under it.
    logger.warning(ls.MG_SET_WITHOUT_BACKEND.format(path=settings.graph_db_file))


def _dialect_for_arguments(addressed: bool) -> GraphDialect:
    configured = get_dialect(settings.GRAPH_BACKEND)
    if not addressed or configured.name == DIALECT_MEMGRAPH:
        return configured
    if configured.name == DIALECT_SQLITE:
        return get_dialect(DIALECT_MEMGRAPH)
    raise ValueError(ex.MEMGRAPH_ADDRESS_UNDER_BACKEND.format(backend=configured.name))


@dataclass(frozen=True, slots=True)
class GraphConnection:
    """Which engine an ingestor opens, besides the Memgraph address: the
    engine's `dialect`, the embedded engine's file (`sqlite_path`, None
    reads SQLITE_GRAPH_PATH at connect) and the Memgraph login."""

    dialect: GraphDialect | None = None
    sqlite_path: str | None = None
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True, slots=True)
class WriteTuning:
    """How an ingestor writes: `batch_size` buffered rows to a flush, and
    nodes by MERGE, or by CREATE without `use_merge`."""

    batch_size: int = 1000
    use_merge: bool = True


class GraphIngestor(GraphBatching, GraphQueries):
    # Its fields are the session's and the batching's, which declare them.
    __slots__ = ()

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        connection: GraphConnection = GraphConnection(),
        tuning: WriteTuning = WriteTuning(),
    ):
        """A graph ingestor for the engine GRAPH_BACKEND selects, or the
        connection's `dialect`.

        A Memgraph address or login passed here names a Memgraph server, so
        without a `dialect` it selects Memgraph even when GRAPH_BACKEND is the
        embedded default, which has no address to apply it to; under Neo4j,
        which connects through NEO4J_URI, it is refused. A field left out is
        read from its MEMGRAPH_* setting.
        """
        dialect, sqlite_path = connection.dialect, connection.sqlite_path
        username, password = connection.username, connection.password
        # The engine is resolved once here rather than read from settings at
        # each call site, so a single ingestor cannot straddle two dialects
        # mid-run if configuration changes underneath it.
        self._dialect = dialect or _dialect_for_arguments(
            addressed=any(v is not None for v in (host, port, username, password))
        )
        # Only the engine the settings chose: a caller naming its own dialect
        # or file picked it on purpose.
        if (
            dialect is None
            and sqlite_path is None
            and self._dialect.name == DIALECT_SQLITE
            and settings.memgraph_set_without_backend
        ):
            _note_memgraph_set_without_backend()
        self._driver = None
        self._driver_lock = threading.Lock()
        # The embedded engine's file; None reads SQLITE_GRAPH_PATH at connect.
        self._sqlite_path = sqlite_path
        self._read_conn = None
        # Guards opening the read connection only: the connection serializes
        # its own statements, so a slow generated query need not hold the
        # lock every write takes.
        self._read_lock = threading.Lock()
        self._host = host if host is not None else settings.MEMGRAPH_HOST
        self._port = port if port is not None else settings.MEMGRAPH_PORT
        if username is None:
            username = settings.MEMGRAPH_USERNAME
        if password is None:
            password = settings.MEMGRAPH_PASSWORD
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
        if tuning.batch_size < 1:
            raise ValueError(ex.BATCH_SIZE)
        self.batch_size = tuning.batch_size
        self._use_merge = tuning.use_merge
        self._conn_lock = threading.Lock()
        self._node_flush_lock = threading.Lock()
        self._executor = None
        self.conn = None
        self.node_buffer = []
        self._rel_count = 0
        self._rel_groups = defaultdict(list)

    def __enter__(self) -> GraphIngestor:
        logger.debug(ls.MG_CONNECTING.format(engine=self.engine, target=self.target))
        self.conn = self._connect()
        # One SQLite file takes one writer at a time: a worker per label
        # would only queue on its lock, so the embedded engine flushes serially.
        if self._dialect.name != DIALECT_SQLITE:
            self._executor = ThreadPoolExecutor(
                max_workers=settings.FLUSH_THREAD_POOL_SIZE
            )
        logger.debug(ls.MG_CONNECTED.format(engine=self._dialect.name))
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
                    # Also at DEBUG: the exception propagates unchanged to a
                    # boundary that reports it (a CLI command, an MCP tool,
                    # the caller), so an ERROR traceback here printed every
                    # failure twice, and undid `cgr export`'s quiet report
                    # (issue #2410).
                    logger.opt(exception=exc_val).debug(
                        ls.MG_EXCEPTION.format(error=exc_val)
                    )
                else:
                    # Ctrl+C or a cancelled task: the user stopped the run,
                    # and a traceback here read as a crash.
                    logger.warning(ls.MG_INTERRUPTED)
                # Best-effort flush: persist buffered nodes/relationships even when
                # an exception occurred. Catch broad Exception so a secondary flush
                # failure never masks the original.
                try:
                    self.flush_all()
                except Exception as flush_err:  # noqa: BLE001 -- cleanup must not mask the original error
                    logger.error(ls.MG_FLUSH_ERROR.format(error=flush_err))
            else:
                self.flush_all()
        finally:
            if self._executor:
                self._executor.shutdown(wait=True)
                self._executor = None
            if self._read_conn:
                self._read_conn.close()
                self._read_conn = None
            if self.conn:
                self.conn.close()
                logger.debug(ls.MG_DISCONNECTED.format(engine=self.engine))
            # Sessions handed to flush workers are closed by those
            # workers; the pooled driver behind them is owned here and
            # would otherwise leak its connection pool for the life of
            # the process.
            self.close_driver()

    async def __aenter__(self) -> GraphIngestor:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)
