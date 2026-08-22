from __future__ import annotations

import types
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from loguru import logger
from neo4j import GraphDatabase, Query

from ... import exceptions as ex
from ... import logs as ls
from ...config import settings
from ...constants import (
    ARCADE_ALLOWED_PROCEDURE_PREFIXES,
    ARCADE_BENIGN_SUBSTRINGS,
    ARCADE_BOLT_SCHEME,
    ARCADE_DDL_EDGE_TYPE,
    ARCADE_DDL_PROPERTY,
    ARCADE_DDL_UNIQUE_INDEX,
    ARCADE_DDL_VERTEX_TYPE,
    ARCADE_PROCEDURE_CATALOG,
    ARCADE_RETRYABLE_SUBSTRINGS,
    NODE_UNIQUE_CONSTRAINTS,
    GraphBackend,
    NodeLabel,
    RelationshipType,
)
from ...types_defs import PropertyValue, ResultRow
from .arcade_http import ArcadeHttpClient

if TYPE_CHECKING:
    from .protocol import GraphIngestor


def build_arcade_schema_statements() -> list[str]:
    """The full DDL bootstrap, ordered so each statement's dependencies exist.

    Derived entirely from the existing constant tables: the guard in
    constants/graph.py that rejects any NodeLabel without a unique key keeps
    this in sync with the schema for free, so there is no second list to
    maintain.
    """
    statements: list[str] = []

    for label in NodeLabel:
        statements.append(ARCADE_DDL_VERTEX_TYPE.format(label=label.value))

    # ArcadeDB refuses CREATE INDEX on an undeclared property, so every
    # property declaration precedes every index.
    for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
        statements.append(ARCADE_DDL_PROPERTY.format(label=label, prop=prop))
    for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
        statements.append(ARCADE_DDL_UNIQUE_INDEX.format(label=label, prop=prop))

    for rel_type in RelationshipType:
        statements.append(ARCADE_DDL_EDGE_TYPE.format(rel_type=rel_type.value))

    return statements


class ArcadeDBDialect:
    __slots__ = ("_http",)

    def __init__(self, http: ArcadeHttpClient | None = None) -> None:
        self._http = http

    @property
    def name(self) -> GraphBackend:
        return GraphBackend.ARCADEDB

    def ensure_schema(self, ingestor: GraphIngestor) -> None:
        if self._http is None:
            raise ex.ArcadeHttpError(ex.ARCADE_NO_HTTP_CLIENT)
        for statement in build_arcade_schema_statements():
            try:
                self._http.sql(statement)
            except Exception as exc:
                if not self.is_benign_error(exc):
                    raise

    def apply_query_limit(self, query: str, mb: int) -> str:
        # No per-query memory cap exists here. ArcadeDBIngestor bounds reads
        # with a transaction timeout instead; see settings.ARCADEDB_TX_TIMEOUT_S.
        return query

    @property
    def procedure_catalog(self) -> str:
        return ARCADE_PROCEDURE_CATALOG

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        return ARCADE_ALLOWED_PROCEDURE_PREFIXES

    def is_benign_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(s in text for s in ARCADE_BENIGN_SUBSTRINGS)

    def is_retryable(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(s in text for s in ARCADE_RETRYABLE_SUBSTRINGS)


class ArcadeDBIngestor:
    """Cypher over Bolt for data; SQL over HTTP for schema DDL.

    ArcadeDB's Bolt listener accepts Cypher only, so the two transports are
    not a choice — index creation is SQL and has nowhere else to go.

    This covers connection lifecycle and the read/write query surface only.
    Batching, flush, and admin operations (clean_database, list_projects,
    delete_project, export_graph_to_dict, ...) are not implemented yet, so
    this class does not yet satisfy the full `GraphIngestor` protocol.
    """

    __slots__ = (
        "_bolt_port",
        "_database",
        "_dialect",
        "_driver",
        "_host",
        "_http",
        "_http_port",
        "_password",
        "_use_merge",
        "_username",
        "batch_size",
    )

    def __init__(
        self,
        host: str,
        bolt_port: int,
        http_port: int,
        database: str,
        username: str,
        password: str,
        batch_size: int = 1000,
        use_merge: bool = True,
    ) -> None:
        # Blank-after-strip is rejected outright, not normalised to "no auth"
        # the way Memgraph's optional credentials are: ArcadeDB's Bolt
        # listener has no unauthenticated fallback, so a whitespace-only
        # credential can never be valid and must fail here, not at connect.
        username = username.strip()
        password = password.strip()
        if not username or not password:
            raise ValueError(ex.ARCADE_CREDENTIALS_REQUIRED)
        if batch_size < 1:
            raise ValueError(ex.BATCH_SIZE)
        self._host = host
        self._bolt_port = bolt_port
        self._http_port = http_port
        self._database = database
        self._username = username
        self._password = password
        self.batch_size = batch_size
        self._use_merge = use_merge
        self._driver: Any | None = None
        self._http = ArcadeHttpClient(
            host=host,
            port=http_port,
            database=database,
            username=username,
            password=password,
        )
        self._dialect = ArcadeDBDialect(http=self._http)

    @property
    def _bolt_uri(self) -> str:
        return f"{ARCADE_BOLT_SCHEME}://{self._host}:{self._bolt_port}"

    def __enter__(self) -> ArcadeDBIngestor:
        logger.info(ls.ARCADE_CONNECTING.format(uri=self._bolt_uri))
        self._driver = GraphDatabase.driver(
            self._bolt_uri, auth=(self._username, self._password)
        )
        logger.info(ls.ARCADE_CONNECTED)
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        if exc_type:
            logger.exception(ls.ARCADE_EXCEPTION.format(error=exc_val))
        if self._driver:
            self._driver.close()
            self._driver = None
            logger.info(ls.ARCADE_DISCONNECTED)

    async def __aenter__(self) -> ArcadeDBIngestor:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)

    @contextmanager
    def _session(self) -> Generator[Any, None, None]:
        # neo4j.Driver is thread-safe and pools internally, so unlike the
        # Memgraph path there is no hand-rolled per-thread connection.
        if self._driver is None:
            raise ConnectionError(ex.ARCADE_NOT_CONNECTED)
        with self._driver.session(database=self._database) as session:
            yield session

    def _run(self, query: str, params: dict[str, Any] | None = None) -> list[ResultRow]:
        # The timeout MUST ride on a Query object. Session.run's signature is
        # run(query, parameters=None, **kwargs), so a bare `timeout=` kwarg
        # would be sent as a Cypher parameter named "timeout" and silently
        # apply no bound at all — and this timeout is the only guard left on
        # runaway LLM-generated queries once QUERY MEMORY LIMIT is gone.
        with self._session() as session:
            # neo4j's stub types Query.text as LiteralString to steer callers
            # away from f-string-interpolated Cypher; every query here is
            # parameterized (see **params below) so a dynamic str is safe,
            # but the stub can't see that.
            result = session.run(
                Query(query, timeout=settings.ARCADEDB_TX_TIMEOUT_S),  # ty: ignore[invalid-argument-type]
                **(params or {}),
            )
            return [dict(record) for record in result]

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        bounded = self._dialect.apply_query_limit(query, settings.QUERY_MEMORY_LIMIT_MB)
        logger.debug(ls.ARCADE_FETCH_QUERY, query=bounded, params=params)
        return self._run(bounded, params)

    def execute_write(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> None:
        logger.debug(ls.ARCADE_WRITE_QUERY, query=query, params=params)
        self._run(query, params)

    def ensure_constraints(self) -> None:
        logger.info(ls.ARCADE_ENSURING_SCHEMA)
        # TODO: remove ty: ignore once ArcadeDBIngestor implements the full
        # GraphIngestor protocol (flush/admin ops land in Tasks 12-13).
        self._dialect.ensure_schema(self)  # ty: ignore[invalid-argument-type]
        logger.info(ls.ARCADE_SCHEMA_DONE)
