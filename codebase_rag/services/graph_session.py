"""The graph ingestor's connection and session handling: which engine it
talks to and where, opening a connection for it, and running one statement
on the shared connection.

`GraphIngestor` (graph_service.py) composes this with its batching and its
query helpers; the state read here is set by its `__init__`.
"""

from __future__ import annotations

import importlib
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING

from loguru import logger

from codebase_rag.config import settings
from codebase_rag.types_defs import ConnectionProtocol, CursorProtocol, ResultValue

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..embedded_graph.connection import EmbeddedConnection
from ..embedded_graph.expressions import CompileError
from ..embedded_graph.lexer import CypherSyntaxError
from ..embedded_graph.store import EmbeddedGraphUnsupported
from ..graph_dialects import DIALECT_NEO4J, DIALECT_SQLITE, GraphDialect
from ..mgclient_compat import MGCLIENT_AVAILABLE, mgclient
from ..stack.constants import MEMGRAPH_AUTH_FAILURE
from ..types_defs import PropertyValue, ResultRow

if TYPE_CHECKING:
    from .neo4j_driver import Neo4jDriver

# pymgclient 1.6 re-exports its C extension through `import *`, which a type
# checker cannot see into, so the exception types are bound once here.
# TransientError first appears in 1.6.0, hence the `pymgclient>=1.6.0` floor.
_MgclientError: type[Exception] = mgclient.Error
_MgclientDatabaseError: type[Exception] = mgclient.DatabaseError
_MgclientOperationalError: type[Exception] = mgclient.OperationalError
_MgclientTransientError: type[Exception] = mgclient.TransientError


def is_query_rejection(error: BaseException) -> bool:
    """Whether the engine refused the query itself, not the connection.

    A rejected query (a syntax or type error such as sorting on a list) can
    be fixed by asking for a different query; an unreachable server or a
    failed login or a missing permission cannot, so those must not trigger a
    regeneration. In mgclient, `OperationalError` (connection) subclasses
    `DatabaseError`; in neo4j, `AuthError` and `Forbidden` subclass
    `ClientError` (issue #2361). The embedded engine raises its own errors
    for a query it cannot parse or compile, and none for a connection.
    """
    if isinstance(error, (CypherSyntaxError, CompileError)):
        return True
    if isinstance(error, _MgclientDatabaseError):
        return not isinstance(error, _MgclientOperationalError)
    try:
        neo4j_exceptions = importlib.import_module(cs.NEO4J_EXCEPTIONS_MODULE)
    except ImportError:
        return False
    return isinstance(error, neo4j_exceptions.ClientError) and not isinstance(
        error, (neo4j_exceptions.AuthError, neo4j_exceptions.Forbidden)
    )


def retry_transient[T](execute: Callable[[], T]) -> T:
    """Run `execute`, again after a doubling wait while Memgraph reports a
    transient conflict (issue #2441).

    The connection autocommits, so a statement rejected with TransientError
    changed nothing, and the error text itself asks for a retry once the
    conflicting transaction is finished. Every other error is raised at once.
    """
    for attempt in range(1, cs.MG_TRANSIENT_RETRY_ATTEMPTS):
        try:
            return execute()
        except _MgclientTransientError as exc:
            delay = cs.MG_TRANSIENT_RETRY_BASE_DELAY_S * 2 ** (attempt - 1)
            logger.warning(
                ls.MG_TRANSIENT_RETRY.format(
                    delay=delay,
                    attempt=attempt,
                    attempts=cs.MG_TRANSIENT_RETRY_ATTEMPTS,
                    error=exc,
                )
            )
            time.sleep(delay)
    return execute()


class GraphUnavailableError(ConnectionError):
    """The graph engine refused the connection or never answered it.

    Raised at connect time with a message that says what to do, so every
    command prints one line instead of mgclient's traceback (#2443).
    """


class GraphSession:
    """Connecting to the configured engine and running one statement on the
    shared connection, under its lock."""

    # Set by `GraphIngestor.__init__`, which says what each one holds.
    __slots__ = (
        "_conn_lock",
        "_dialect",
        "_driver",
        "_driver_lock",
        "_host",
        "_password",
        "_port",
        "_read_conn",
        "_read_lock",
        "_sqlite_path",
        "_username",
        "conn",
    )

    _dialect: GraphDialect
    _host: str
    _port: int
    _username: str | None
    _password: str | None
    _sqlite_path: str | None
    _driver: Neo4jDriver | None
    _driver_lock: threading.Lock
    _read_conn: EmbeddedConnection | None
    _read_lock: threading.Lock
    _conn_lock: threading.Lock
    conn: ConnectionProtocol | None

    @property
    def engine(self) -> str:
        """The graph engine this ingestor talks to."""
        return self._dialect.name

    @property
    def target(self) -> str:
        """Where its graph lives: the embedded file, the Neo4j URI, or the
        Memgraph address."""
        if self._dialect.name == DIALECT_SQLITE:
            return self.sqlite_graph_path()
        if self._dialect.name == DIALECT_NEO4J:
            return settings.NEO4J_URI
        return f"{self._host}:{self._port}"

    def _unavailable_message(self, error: Exception) -> str:
        # Memgraph refuses a login with the same exception type as a refused
        # connection, so only the message tells the two apart.
        address = f"{self._host}:{self._port}"
        if MEMGRAPH_AUTH_FAILURE in str(error):
            return ex.GRAPH_CREDENTIALS_REFUSED.format(address=address, error=error)
        return ex.GRAPH_UNREACHABLE.format(address=address, error=error)

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
                retry_transient(partial(cursor.execute, query, params))
                return self._cursor_to_results(cursor)
            except Exception as e:
                # Only "already exists" is expected (re-running schema DDL);
                # a constraint violation is a write that did not happen.
                if cs.ERR_SUBSTR_ALREADY_EXISTS not in str(e).lower():
                    logger.error(ls.MG_CYPHER_ERROR.format(error=e))
                    logger.error(ls.MG_CYPHER_QUERY.format(query=query))
                    logger.error(ls.MG_CYPHER_PARAMS.format(params=params))
                raise

    def _connect(self) -> ConnectionProtocol:
        """`open_connection`, with a refused or unanswered Memgraph
        connection raised as a `GraphUnavailableError` that says what to do."""
        try:
            return self.open_connection()
        except GraphUnavailableError:
            raise
        except _MgclientError as e:
            raise GraphUnavailableError(self._unavailable_message(e)) from e
        except Exception as e:
            # The driver's error does not say where it tried to connect, and
            # the line that did is DEBUG now (issue #2398).
            logger.error(
                ls.MG_CONNECT_FAILED.format(host=self._host, port=self._port, error=e)
            )
            raise

    def open_connection(self) -> ConnectionProtocol:
        """Open one connection for the configured engine.

        The parallel flush calls this once per worker, so whatever comes
        back must be safe to use from a single thread and cheap enough to
        create per flush group. For Neo4j that is a session drawn from a
        shared, pooled `Driver`; for Memgraph it is a real socket.
        """
        if self._dialect.name == DIALECT_NEO4J:
            return self._neo4j_driver().connect()
        if self._dialect.name == DIALECT_SQLITE:
            return self._embedded_connection(read_only=False)
        if not MGCLIENT_AVAILABLE:
            raise GraphUnavailableError(ex.MGCLIENT_MISSING)
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

    def sqlite_graph_path(self) -> str:
        return self._sqlite_path or str(settings.graph_db_file)

    def _embedded_connection(
        self, read_only: bool, heap_limit_mb: int | None = None
    ) -> EmbeddedConnection:
        path = self.sqlite_graph_path()
        try:
            return EmbeddedConnection(path, read_only, heap_limit_mb)
        except (sqlite3.Error, OSError, EmbeddedGraphUnsupported) as e:
            raise GraphUnavailableError(
                ex.EMBEDDED_GRAPH_UNAVAILABLE.format(path=path, error=e)
            ) from e

    def _read_only_connection(self) -> EmbeddedConnection:
        """The embedded file's read-only connection, opened on first use with
        SQLite's heap capped at QUERY_MEMORY_LIMIT_MB."""
        with self._read_lock:
            if self._read_conn is None:
                self._read_conn = self._embedded_connection(
                    read_only=True, heap_limit_mb=settings.QUERY_MEMORY_LIMIT_MB
                )
            return self._read_conn

    def close_driver(self) -> None:
        """Release the Neo4j connection pool, if one was ever built.

        Sessions handed to flush workers are closed by those workers; the
        pooled driver behind them is owned by this object and would
        otherwise outlive it. A Memgraph run never builds one, so this is
        a no-op there.
        """
        if self._driver is not None:
            self._driver.close()
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
            return self._driver
