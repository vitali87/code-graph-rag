from __future__ import annotations

import types
from collections import defaultdict
from collections.abc import Generator, Hashable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
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
    ARCADE_HTTP_SCHEME,
    ARCADE_PROCEDURE_CATALOG,
    ARCADE_RETRYABLE_SUBSTRINGS,
    CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES,
    KEY_NAME,
    KEY_PROJECT_NAME,
    MERGE_KEY_PROPS_BY_REL,
    NODE_UNIQUE_CONSTRAINTS,
    ArcadeHttpScheme,
    GraphBackend,
    NodeLabel,
    RelationshipType,
)
from ...cypher_queries import (
    CYPHER_DELETE_ALL,
    CYPHER_DELETE_PROJECT,
    CYPHER_EXPORT_NODES,
    CYPHER_EXPORT_RELATIONSHIPS,
    CYPHER_LIST_PROJECTS,
    build_create_node_query,
    build_create_relationship_query,
    build_merge_node_query,
    build_merge_relationship_query,
    wrap_with_unwind,
)
from ...types_defs import (
    BatchParams,
    GraphData,
    GraphMetadata,
    NodeBatchRow,
    PropertyValue,
    RelBatchRow,
    ResultRow,
)
from ...utils.path_utils import project_roots_from_rows
from ..resource_cleanup import prune_unanchored_resources
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


def _hashable(value: PropertyValue) -> Hashable:
    # PropertyValue includes list[str] (e.g. decorators), which can't sit in
    # a dict key as-is.
    return tuple(value) if isinstance(value, list) else value


def _chunk_endpoint_disjoint(rows: list[RelBatchRow]) -> list[list[RelBatchRow]]:
    """Partition rows into UNWIND-safe chunks: no chunk contains two rows
    sharing an endpoint (from_val or to_val).

    Confirmed by an isolated probe against a live server: 300 MERGEs onto
    one hot vertex succeeded 300/300 sent one row at a time, but the
    identical 300 rows sent as a single UNWIND batch deadlocked
    deterministically -- and indexing this repo's own test suite showed
    the failure mode is not always a raised exception either: 9 of 508
    CALLS edges onto the shared `load_parsers` target vanished from a
    batched flush with no error and no attempted/created mismatch in this
    ingestor's own bookkeeping. Retrying or bisecting an already-sent
    UNWIND cannot rule that out -- it can only reduce how often it is hit.
    Guaranteeing every chunk's endpoints are pairwise disjoint before
    anything is sent removes the collision outright, and costs nothing
    for the common case (unique targets per row all land in one chunk);
    only a genuinely hot vertex degrades to one row per chunk.
    """
    chunks: list[list[RelBatchRow]] = []
    chunk_endpoints: list[set[Hashable]] = []
    for row in rows:
        from_h = _hashable(row["from_val"])
        to_h = _hashable(row["to_val"])
        for chunk, endpoints in zip(chunks, chunk_endpoints, strict=True):
            if from_h not in endpoints and to_h not in endpoints:
                chunk.append(row)
                endpoints.add(from_h)
                endpoints.add(to_h)
                break
        else:
            chunks.append([row])
            chunk_endpoints.append({from_h, to_h})
    return chunks


def _dedupe_rows_sharing_a_merge_pattern(
    rows: list[RelBatchRow], merge_key_props: tuple[str, ...]
) -> list[RelBatchRow]:
    """Collapse rows that MERGE onto the identical (endpoints + key props)
    pattern before they reach the server.

    Relationships carry no unique index on ArcadeDB (only vertex types do),
    so its Cypher MERGE only checks already-committed state -- it cannot see
    an earlier row's write from *this same* UNWIND-batched statement. Two
    rows with an otherwise-identical MERGE pattern therefore created two
    edges on ArcadeDB where Memgraph's engine naturally converged on one
    (found via manual probing while building the Task 14 conformance suite;
    see TestRelationships.test_merge_does_not_duplicate_the_same_edge_within_one_batch
    in test_graph_backend_conformance.py). Pre-merging duplicates
    client-side -- overlaying each row's props onto the running merge
    per-key, later values winning only on the keys they actually carry,
    never wiping keys a later row omits -- mirrors Memgraph's own
    SET-after-MERGE overlay exactly (NOT a blunt "keep the last row",
    which would silently drop any prop only an earlier row set), so the two
    engines converge on the same graph regardless of engine-level MERGE
    semantics.
    """
    merged: dict[tuple[Hashable, Hashable, tuple[Hashable, ...]], RelBatchRow] = {}
    for row in rows:
        props = row["props"] or {}
        key = (
            _hashable(row["from_val"]),
            _hashable(row["to_val"]),
            tuple(_hashable(props.get(p)) for p in merge_key_props),
        )
        prior = merged.get(key)
        if prior is None:
            merged[key] = row
        else:
            merged[key] = RelBatchRow(
                from_val=row["from_val"],
                to_val=row["to_val"],
                props={**(prior["props"] or {}), **props},
            )
    return list(merged.values())


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

    This covers connection lifecycle, the read/write query surface, the
    buffered ingestion path (ensure_*_batch / flush_*), and the admin
    operations (clean_database, list_projects, delete_project,
    export_graph_to_dict, ...) -- together the full `GraphIngestor`
    protocol.
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
        http_scheme: ArcadeHttpScheme | str = ARCADE_HTTP_SCHEME,
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
            scheme=http_scheme,
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
        self._dialect.ensure_schema(self)
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
            # Defense in depth, not the primary defense: every caller now
            # runs rows through _chunk_endpoint_disjoint first, so a batch
            # that reaches this point already has no two rows sharing an
            # endpoint and this branch should rarely trigger. It stays
            # because retry_on_transient can still exhaust its budget on
            # ordinary external MVCC contention (two concurrently-flushing
            # relationship-pattern groups both touching a shared vertex --
            # see flush_relationships), and confirmed by an isolated probe
            # against a live server: 300 MERGEs onto one hot vertex, sent as
            # 300 separate single-row queries, succeeded 300/300 with zero
            # conflicts, so degrading to one MERGE per row here is a safe
            # bottom rung regardless of why the batch failed.
            if len(rows) > 1 and self._dialect.is_retryable(e):
                results: list[ResultRow] = []
                for row in rows:
                    results.extend(self._execute_batch(query, [row]))
                return results
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
    ) -> tuple[int, int, int]:
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
            results: list[ResultRow] = []
            for chunk in _chunk_endpoint_disjoint(rows):
                results.extend(self._execute_batch(query, chunk))
            return len(rows), _count_created(results), 0

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
        deduped_away = 0
        for merge_key_props, group in by_keys.items():
            deduped = _dedupe_rows_sharing_a_merge_pattern(group, merge_key_props)
            # Duplicate rows collapsed here are the expected, common case
            # this dedup exists for (see _dedupe_rows_sharing_a_merge_pattern)
            # -- count what was actually SENT so `attempted - created` in
            # flush_relationships' log line reflects genuine failures
            # (e.g. an endpoint not yet in the graph), not rows this method
            # itself chose to collapse.
            deduped_away += len(group) - len(deduped)
            query = build_merge_relationship_query(
                from_label,
                from_key,
                rel_type,
                to_label,
                to_key,
                any(r["props"] for r in deduped),
                merge_key_props=merge_key_props,
            )
            merge_results: list[ResultRow] = []
            for chunk in _chunk_endpoint_disjoint(deduped):
                merge_results.extend(self._execute_batch(query, chunk))
            attempted += len(deduped)
            created += _count_created(merge_results)
        return attempted, created, deduped_away

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
        deduped_away = 0
        first_error: Exception | None = None

        # Fans out across the executor exactly like flush_nodes: different
        # (from_label, rel_type, to_label) patterns commonly share an
        # endpoint (most rel types in this schema hang off Module, for
        # example), so two groups' concurrent MERGEs onto that shared
        # vertex is real, expected ArcadeDB MVCC contention -- the
        # "ConcurrentModification"/"Transaction" errors retry.py's
        # is_retryable already exists to retry. That is a different failure
        # from the one _chunk_endpoint_disjoint (see its docstring) exists
        # for: a single UNWIND batch where 2+ *rows in the same call* share
        # an endpoint, which this project found can silently drop a row
        # instead of raising anything retryable at all. Chunking each
        # group's rows before they are ever sent removes that hazard
        # regardless of how many groups run at once, so fanning back out
        # across groups here is safe -- and restores what
        # test_parallel_flush_into_one_hot_target documents itself as
        # covering: real concurrent writes at the database layer, not just
        # concurrent buffer appends.
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
                    a, c, d = future.result()
                    attempted += a
                    created += c
                    deduped_away += d
                except Exception as e:
                    logger.error(
                        ls.ARCADE_REL_FLUSH_ERROR.format(pattern=pattern, error=e)
                    )
                    first_error = first_error or e
        else:
            for pattern, rows in self._rel_groups.items():
                try:
                    a, c, d = self._flush_rel_pattern_group(pattern, rows)
                    attempted += a
                    created += c
                    deduped_away += d
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
        # Logged distinctly from "failed" above: these rows were never sent
        # to the server at all -- they're the same-batch duplicates
        # _dedupe_rows_sharing_a_merge_pattern collapsed before the MERGE,
        # not query failures. Duplicate CALLS/etc. edges within one flush
        # cycle are the expected common case (see issue found in Task 14),
        # so this is routine, not a warning.
        if deduped_away:
            logger.info(ls.ARCADE_RELS_DEDUPED.format(count=deduped_away))
        self._rel_count = 0
        self._rel_groups.clear()
        if first_error is not None:
            raise first_error

    def flush_all(self) -> None:
        self.flush_nodes()
        self.flush_relationships()

    def clean_database(self) -> None:
        logger.info(ls.ARCADE_CLEANING_DB)
        # DETACH DELETE clears records but leaves ArcadeDB's type definitions
        # behind. That is fine: ensure_schema is idempotent and reuses them.
        self.execute_write(CYPHER_DELETE_ALL)
        logger.info(ls.ARCADE_DB_CLEANED)

    def list_projects(self) -> list[str]:
        return [str(r[KEY_NAME]) for r in self.fetch_all(CYPHER_LIST_PROJECTS)]

    def list_project_roots(self) -> dict[str, str | None]:
        return project_roots_from_rows(self.fetch_all(CYPHER_LIST_PROJECTS))

    def delete_project(self, project_name: str) -> None:
        logger.info(ls.ARCADE_DELETING_PROJECT.format(project_name=project_name))
        self.execute_write(CYPHER_DELETE_PROJECT, {KEY_PROJECT_NAME: project_name})
        # Shared prefix-less nodes (Resources, ExternalModules) only lose
        # their edges above; drop the ones this project alone anchored.
        prune_unanchored_resources(self)
        self.execute_write(CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
        logger.info(ls.ARCADE_PROJECT_DELETED.format(project_name=project_name))

    def export_graph_to_dict(self) -> GraphData:
        nodes_data = self.fetch_all(CYPHER_EXPORT_NODES)
        relationships_data = self.fetch_all(CYPHER_EXPORT_RELATIONSHIPS)
        return GraphData(
            nodes=nodes_data,
            relationships=relationships_data,
            metadata=GraphMetadata(
                total_nodes=len(nodes_data),
                total_relationships=len(relationships_data),
                exported_at=datetime.now(UTC).isoformat(),
            ),
        )
