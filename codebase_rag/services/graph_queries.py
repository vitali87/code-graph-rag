"""The graph ingestor's query helpers: project listing and deletion, the
schema (constraints, indexes and the legacy-key migration), reads (bounded,
or read-only for an untrusted query), writes and the export.

`GraphIngestor` (graph_service.py) composes this with its session and its
batching; the state read here is set by its `__init__`.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from loguru import logger

from codebase_rag.config import settings
from codebase_rag.types_defs import ResultValue

from .. import constants as cs
from .. import logs as ls
from ..cypher_queries import (
    CYPHER_ANY_KEYLESS_STRUCTURE,
    CYPHER_ANY_SHARED_STRUCTURE,
    CYPHER_DELETE_ALL,
    CYPHER_DELETE_PROJECT,
    CYPHER_DELETE_PROJECT_CUT_OFF_UNITS,
    CYPHER_EXPORT_NODES,
    CYPHER_EXPORT_PROJECT_NODES,
    CYPHER_EXPORT_PROJECT_RELATIONSHIPS,
    CYPHER_EXPORT_RELATIONSHIPS,
    CYPHER_LIST_PROJECTS,
    CYPHER_PURGE_CROSS_PROJECT_STRUCTURE,
    CYPHER_PURGE_CROSS_ROOT_STRUCTURE,
    CYPHER_PURGE_KEYLESS_STRUCTURE,
)
from ..graph_dialects import DIALECT_NEO4J, DIALECT_SQLITE, GraphDialect, get_dialect
from ..types_defs import GraphData, GraphMetadata, PropertyParams, ResultRow
from ..utils.path_utils import project_roots_from_rows
from .cypher_guard import check_plan, memgraph_plan_operators, neo4j_plan_operators
from .graph_session import GraphSession
from .resource_cleanup import prune_unanchored_resources


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


class GraphQueries(GraphSession):
    """The statements the ingestor runs on the graph beyond its batches."""

    # Stateless: every field it reads is the session's.
    __slots__ = ()

    def clean_database(self) -> None:
        logger.info(ls.MG_CLEANING_DB)
        self._execute_query(CYPHER_DELETE_ALL)
        logger.info(ls.MG_DB_CLEANED)

    def list_projects(self) -> list[str]:
        result = self.fetch_all(CYPHER_LIST_PROJECTS)
        return [str(r[cs.KEY_NAME]) for r in result]

    def list_project_roots(self) -> dict[str, str | None]:
        return project_roots_from_rows(self.fetch_all(CYPHER_LIST_PROJECTS))

    def delete_project(self, project_name: str) -> None:
        logger.info(ls.MG_DELETING_PROJECT.format(project_name=project_name))
        self._execute_query(CYPHER_DELETE_PROJECT, {cs.KEY_PROJECT_NAME: project_name})
        self._execute_query(
            CYPHER_DELETE_PROJECT_CUT_OFF_UNITS,
            {
                cs.KEY_PROJECT_NAME: project_name,
                cs.KEY_PROJECT_PREFIX: f"{project_name}{cs.SEPARATOR_DOT}",
            },
        )
        # Shared prefix-less nodes (Resources, ExternalModules) only lose
        # their edges above; drop the ones this project alone anchored.
        prune_unanchored_resources(self)
        self._execute_query(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
        logger.info(ls.MG_PROJECT_DELETED.format(project_name=project_name))

    def ensure_constraints(self) -> None:
        logger.info(ls.MG_ENSURING_CONSTRAINTS)
        self._migrate_legacy_path_keys()
        failed = self._apply_schema_ddl(
            self._dialect.create_constraint(label, prop)
            for label, prop in cs.NODE_UNIQUE_CONSTRAINTS.items()
        )
        if failed:
            logger.warning(ls.MG_CONSTRAINTS_INCOMPLETE.format(count=failed))
        else:
            logger.info(ls.MG_CONSTRAINTS_DONE)
        self._ensure_indexes()

    def _apply_schema_ddl(self, statements: Iterable[str]) -> int:
        """Run each DDL statement and return how many failed.

        None raises: a missing constraint or index must not stop ingestion.
        `_execute_query` logs each failure but one saying the object already
        exists, which is the expected answer once the schema is in place.
        """
        failed = 0
        for statement in statements:
            try:
                self._execute_query(statement)
            except Exception as e:  # noqa: BLE001 -- any engine's DDL error; an existing index is no failure
                if cs.ERR_SUBSTR_ALREADY_EXISTS not in str(e).lower():
                    failed += 1
        return failed

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
        for label, prop in cs.LEGACY_NODE_CONSTRAINTS:
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
        # With a legacy constraint in force no current sync has written here,
        # so every shared node is the old key's merge. Without one, current
        # data shares a node between projects over the same files, and only a
        # node outside an owner's tree is legacy (issue #3025).
        if legacy_present:
            cross_project_purge = CYPHER_PURGE_CROSS_PROJECT_STRUCTURE
            damaged = True
        else:
            cross_project_purge = CYPHER_PURGE_CROSS_ROOT_STRUCTURE
            damaged = bool(self._execute_query(CYPHER_ANY_SHARED_STRUCTURE)) or bool(
                self._execute_query(CYPHER_ANY_KEYLESS_STRUCTURE)
            )
        if not damaged:
            return
        purged = 0
        for purge_query in (cross_project_purge, CYPHER_PURGE_KEYLESS_STRUCTURE):
            rows = self._execute_query(purge_query)
            if rows:
                purged += int(str(rows[0][cs.KEY_PURGED]))
        if purged:
            logger.warning(ls.MG_LEGACY_PURGE.format(count=purged))

    def _ensure_indexes(self) -> None:
        logger.info(ls.MG_ENSURING_INDEXES)
        # The unique-key indexes serve MERGE at write time; generated Cypher
        # reads filter on bare `name`, which needs its own label+name index
        # or every lookup is a full label scan.
        failed = self._apply_schema_ddl(
            [
                *(
                    self._dialect.create_index(label, prop)
                    for label, prop in cs.NODE_UNIQUE_CONSTRAINTS.items()
                ),
                *(
                    self._dialect.create_index(label, cs.KEY_NAME)
                    for label in cs.NODE_NAME_INDEXES
                ),
            ]
        )
        if failed:
            logger.warning(ls.MG_INDEXES_INCOMPLETE.format(count=failed))
        else:
            logger.info(ls.MG_INDEXES_DONE)

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
        plan = self._execute_query(cs.CYPHER_EXPLAIN_PREFIX + bounded_query)
        check_plan(
            memgraph_plan_operators(
                [str(next(iter(row.values()), "")) for row in plan]
            ),
            query,
        )
        if self._dialect.name == DIALECT_SQLITE:
            return self._fetch_embedded_read_only(bounded_query)
        return self._execute_query(bounded_query)

    def _fetch_embedded_read_only(self, query: str) -> list[ResultRow]:
        """Run a checked query on a connection SQLite itself opened read-only.

        Unlike Neo4j's READ session this is access control: the file is
        opened `mode=ro`, so a write the plan check missed fails in SQLite.
        An in-memory graph has no file to reopen and uses the main
        connection, where the plan check is the guard.

        The engine runs in this process, so the bounds the server enforces
        for Memgraph are enforced here: the statement stops itself at
        QUERY_TIMEOUT_S (the caller's own timeout only stops waiting), and
        SQLite's heap is capped at QUERY_MEMORY_LIMIT_MB.
        """
        if self.sqlite_graph_path() == cs.EG_MEMORY_PATH:
            return self._execute_query(query)
        result = self._read_only_connection().run(
            query, timeout_s=settings.QUERY_TIMEOUT_S
        )
        return [dict[str, ResultValue](zip(result.columns, row)) for row in result.rows]

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
            params: PropertyParams = {cs.KEY_PROJECT_NAMES: list(project_names)}
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
