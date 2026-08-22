from __future__ import annotations

import types
from collections import defaultdict
from collections.abc import Generator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    MERGE_KEY_PROPS_BY_REL,
    NODE_UNIQUE_CONSTRAINTS,
    GraphBackend,
    NodeLabel,
    RelationshipType,
)
from ...cypher_queries import (
    build_create_node_query,
    build_create_relationship_query,
    build_merge_node_query,
    build_merge_relationship_query,
    wrap_with_unwind,
)
from ...types_defs import (
    BatchParams,
    NodeBatchRow,
    PropertyValue,
    RelBatchRow,
    ResultRow,
)
from .arcade_http import ArcadeHttpClient
from .retry import retry_on_transient

if TYPE_CHECKING:
    from .protocol import GraphIngestor


def _count_created(results: list[ResultRow]) -> int:
    # ResultRow values are the broad ResultValue union (list/dict included),
    # so int() on the raw value doesn't type-check; narrow with isinstance
    # instead, matching MemgraphIngestor._flush_rel_pattern_group.
    total = 0
    for row in results:
        created = row.get("created", 0)
        if isinstance(created, int):
            total += created
    return total


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

    This covers connection lifecycle, the read/write query surface, and the
    buffered ingestion path (ensure_*_batch / flush_*). Admin operations
    (clean_database, list_projects, delete_project, export_graph_to_dict,
    ...) are not implemented yet, so this class does not yet satisfy the
    full `GraphIngestor` protocol.
    """

    __slots__ = (
        "_bolt_port",
        "_database",
        "_dialect",
        "_driver",
        "_executor",
        "_host",
        "_http",
        "_http_port",
        "_password",
        "_rel_count",
        "_rel_groups",
        "_use_merge",
        "_username",
        "batch_size",
        "node_buffer",
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
        self._executor: ThreadPoolExecutor | None = None
        self.node_buffer: list[tuple[str, dict[str, PropertyValue]]] = []
        self._rel_count = 0
        self._rel_groups: defaultdict[
            tuple[str, str, str, str, str], list[RelBatchRow]
        ] = defaultdict(list)
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
        self._executor = ThreadPoolExecutor(max_workers=settings.FLUSH_THREAD_POOL_SIZE)
        logger.info(ls.ARCADE_CONNECTED)
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        try:
            if exc_type:
                logger.exception(ls.ARCADE_EXCEPTION.format(error=exc_val))
                # Best-effort flush: persist buffered nodes/relationships even
                # when an exception occurred. Catch broad Exception so a
                # secondary flush failure never masks the original.
                try:
                    self.flush_all()
                except Exception as flush_err:
                    logger.error(ls.ARCADE_FLUSH_ERROR.format(error=flush_err))
            else:
                self.flush_all()
        finally:
            if self._executor:
                self._executor.shutdown(wait=True)
                self._executor = None
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

    def ensure_node_batch(
        self, label: str, properties: dict[str, PropertyValue]
    ) -> None:
        self.node_buffer.append((label, properties))
        if len(self.node_buffer) >= self.batch_size:
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
            self.flush_nodes()
            self.flush_relationships()

    def _execute_batch(
        self, query: str, rows: Sequence[BatchParams]
    ) -> list[ResultRow]:
        if not rows:
            return []

        def run() -> list[ResultRow]:
            with self._session() as session:
                # See the identical LiteralString stub note in _run above.
                unwound = wrap_with_unwind(query)
                result = session.run(
                    Query(unwound, timeout=settings.ARCADEDB_TX_TIMEOUT_S),  # ty: ignore[invalid-argument-type]
                    batch=list(rows),
                )
                return [dict(record) for record in result]

        try:
            return retry_on_transient(run, self._dialect)
        except Exception as e:
            if not self._dialect.is_benign_error(e):
                logger.error(ls.ARCADE_BATCH_ERROR.format(error=e))
                logger.error(ls.ARCADE_CYPHER_QUERY.format(query=query))
            raise

    def _flush_node_label_group(
        self, label: str, props_list: list[dict[str, PropertyValue]]
    ) -> tuple[int, int]:
        id_key = NODE_UNIQUE_CONSTRAINTS.get(label)
        if not id_key:
            logger.warning(ls.ARCADE_NO_CONSTRAINT.format(label=label))
            return 0, len(props_list)

        rows: list[NodeBatchRow] = []
        skipped = 0
        for props in props_list:
            if id_key not in props:
                skipped += 1
                continue
            rows.append(
                NodeBatchRow(
                    id=props[id_key],
                    props={k: v for k, v in props.items() if k != id_key},
                )
            )
        if not rows:
            return 0, skipped

        build = build_merge_node_query if self._use_merge else build_create_node_query
        self._execute_batch(build(label, id_key), rows)
        return len(rows), skipped

    def _flush_rel_pattern_group(
        self,
        pattern: tuple[str, str, str, str, str],
        rows: list[RelBatchRow],
    ) -> tuple[int, int]:
        from_label, from_key, rel_type, to_label, to_key = pattern

        if not self._use_merge:
            query = build_create_relationship_query(
                from_label,
                from_key,
                rel_type,
                to_label,
                to_key,
                any(r["props"] for r in rows),
            )
            results = self._execute_batch(query, rows)
            return len(rows), _count_created(results)

        # Issue #722: rows for the same endpoints may carry different
        # distinguishing props. Flushing each merge-key signature separately
        # stops a prop absent from one row being dropped from the key for the
        # rest, which would re-collapse parallel provenance edges.
        candidate = MERGE_KEY_PROPS_BY_REL.get(rel_type, ())
        by_keys: defaultdict[tuple[str, ...], list[RelBatchRow]] = defaultdict(list)
        for row in rows:
            props = row["props"] or {}
            by_keys[tuple(p for p in candidate if p in props)].append(row)

        attempted = 0
        created = 0
        for merge_key_props, group in by_keys.items():
            query = build_merge_relationship_query(
                from_label,
                from_key,
                rel_type,
                to_label,
                to_key,
                any(r["props"] for r in group),
                merge_key_props=merge_key_props,
            )
            results = self._execute_batch(query, group)
            attempted += len(group)
            created += _count_created(results)
        return attempted, created

    def flush_nodes(self) -> None:
        if not self.node_buffer:
            return
        by_label: defaultdict[str, list[dict[str, PropertyValue]]] = defaultdict(list)
        for label, props in self.node_buffer:
            by_label[label].append(props)

        total = len(self.node_buffer)
        flushed = 0
        first_error: Exception | None = None

        # neo4j.Driver pools sessions internally, so unlike the Memgraph path
        # there is no per-group connection to create and close — each worker
        # just calls _execute_batch, which takes a session from the pool.
        if self._executor and len(by_label) > 1:
            futures = {
                self._executor.submit(self._flush_node_label_group, label, props): label
                for label, props in by_label.items()
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    count, _ = future.result()
                    flushed += count
                except Exception as e:
                    logger.error(
                        ls.ARCADE_LABEL_FLUSH_ERROR.format(label=label, error=e)
                    )
                    first_error = first_error or e
        else:
            for label, props in by_label.items():
                try:
                    count, _ = self._flush_node_label_group(label, props)
                    flushed += count
                except Exception as e:
                    logger.error(
                        ls.ARCADE_LABEL_FLUSH_ERROR.format(label=label, error=e)
                    )
                    first_error = first_error or e

        logger.info(ls.ARCADE_NODES_FLUSHED.format(flushed=flushed, total=total))
        self.node_buffer.clear()
        if first_error is not None:
            raise first_error

    def flush_relationships(self) -> None:
        if not self._rel_count:
            return
        total = self._rel_count
        attempted = 0
        created = 0
        first_error: Exception | None = None

        if self._executor and len(self._rel_groups) > 1:
            futures = {
                self._executor.submit(
                    self._flush_rel_pattern_group, pattern, rows
                ): pattern
                for pattern, rows in self._rel_groups.items()
            }
            for future in as_completed(futures):
                pattern = futures[future]
                try:
                    a, c = future.result()
                    attempted += a
                    created += c
                except Exception as e:
                    logger.error(
                        ls.ARCADE_REL_FLUSH_ERROR.format(pattern=pattern, error=e)
                    )
                    first_error = first_error or e
        else:
            for pattern, rows in self._rel_groups.items():
                try:
                    a, c = self._flush_rel_pattern_group(pattern, rows)
                    attempted += a
                    created += c
                except Exception as e:
                    logger.error(
                        ls.ARCADE_REL_FLUSH_ERROR.format(pattern=pattern, error=e)
                    )
                    first_error = first_error or e

        logger.info(
            ls.ARCADE_RELS_FLUSHED.format(
                total=total, success=created, failed=attempted - created
            )
        )
        self._rel_count = 0
        self._rel_groups.clear()
        if first_error is not None:
            raise first_error

    def flush_all(self) -> None:
        self.flush_nodes()
        self.flush_relationships()
