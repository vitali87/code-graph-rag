from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

import mgclient  # ty: ignore[unresolved-import]
from loguru import logger

from codebase_rag.types_defs import CursorProtocol, ResultValue

from .. import exceptions as ex
from .. import logs as ls
from ..constants import (
    ERR_SUBSTR_ALREADY_EXISTS,
    ERR_SUBSTR_CONSTRAINT,
    KEY_CREATED,
    KEY_FROM_VAL,
    KEY_NAME,
    KEY_PROJECT_NAME,
    KEY_PROPS,
    KEY_TO_VAL,
    NODE_UNIQUE_CONSTRAINTS,
    REL_TYPE_CALLS,
)
from ..cypher_queries import (
    CYPHER_DELETE_ALL,
    CYPHER_DELETE_PROJECT,
    CYPHER_EXPORT_NODES,
    CYPHER_EXPORT_RELATIONSHIPS,
    CYPHER_LIST_PROJECTS,
    build_constraint_query,
    build_index_query,
    build_merge_node_query,
    build_merge_relationship_query,
    wrap_with_unwind,
)
from ..types_defs import (
    BatchParams,
    BatchWrapper,
    GraphData,
    GraphMetadata,
    NodeBatchRow,
    PropertyDict,
    PropertyValue,
    RelBatchRow,
    ResultRow,
)


class MemgraphIngestor:
    def __init__(self, host: str, port: int, batch_size: int = 1000):
        self._host = host
        self._port = port
        if batch_size < 1:
            raise ValueError(ex.BATCH_SIZE)
        self.batch_size = batch_size
        self.conn: mgclient.Connection | None = None
        self.node_buffer: list[tuple[str, dict[str, PropertyValue]]] = []
        self.relationship_buffer: list[
            tuple[
                tuple[str, str, PropertyValue],
                str,
                tuple[str, str, PropertyValue],
                dict[str, PropertyValue] | None,
            ]
        ] = []

    def __enter__(self) -> "MemgraphIngestor":
        logger.info(ls.MG_CONNECTING.format(host=self._host, port=self._port))
        self.conn = mgclient.connect(host=self._host, port=self._port)
        self.conn.autocommit = True
        logger.info(ls.MG_CONNECTED)
        return self

    def __exit__(
        self, exc_type: type | None, exc_val: Exception | None, exc_tb: object
    ) -> None:
        if exc_type:
            logger.exception(ls.MG_EXCEPTION.format(error=exc_val))
        self.flush_all()
        if self.conn:
            self.conn.close()
            logger.info(ls.MG_DISCONNECTED)

    @contextmanager
    def _get_cursor(self) -> Generator[CursorProtocol, None, None]:
        if not self.conn:
            raise ConnectionError(ex.CONN)
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

    def _execute_batch(self, query: str, params_list: Sequence[BatchParams]) -> None:
        if not self.conn or not params_list:
            return
        cursor = None
        try:
            cursor = self.conn.cursor()
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

    def _execute_batch_with_return(
        self, query: str, params_list: Sequence[BatchParams]
    ) -> list[ResultRow]:
        if not self.conn or not params_list:
            return []
        cursor = None
        try:
            cursor = self.conn.cursor()
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

    def delete_project(self, project_name: str) -> None:
        logger.info(ls.MG_DELETING_PROJECT.format(project_name=project_name))
        self._execute_query(CYPHER_DELETE_PROJECT, {KEY_PROJECT_NAME: project_name})
        logger.info(ls.MG_PROJECT_DELETED.format(project_name=project_name))

    def ensure_constraints(self) -> None:
        logger.info(ls.MG_ENSURING_CONSTRAINTS)
        for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
            try:
                self._execute_query(build_constraint_query(label, prop))
            except Exception:
                pass
        logger.info(ls.MG_CONSTRAINTS_DONE)
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        logger.info(ls.MG_ENSURING_INDEXES)
        for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
            try:
                self._execute_query(build_index_query(label, prop))
            except Exception:
                pass
        logger.info(ls.MG_INDEXES_DONE)

    def ensure_node_batch(
        self, label: str, properties: dict[str, PropertyValue]
    ) -> None:
        self.node_buffer.append((label, properties))
        if len(self.node_buffer) >= self.batch_size:
            logger.debug(ls.MG_NODE_BUFFER_FLUSH.format(size=self.batch_size))
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
        self.relationship_buffer.append(
            (
                (from_label, from_key, from_val),
                rel_type,
                (to_label, to_key, to_val),
                properties,
            )
        )
        if len(self.relationship_buffer) >= self.batch_size:
            logger.debug(ls.MG_REL_BUFFER_FLUSH.format(size=self.batch_size))
            self.flush_nodes()
            self.flush_relationships()

    def flush_nodes(self) -> None:
        if not self.node_buffer:
            return

        buffer_size = len(self.node_buffer)
        nodes_by_label: defaultdict[str, list[dict[str, PropertyValue]]] = defaultdict(
            list
        )
        for label, props in self.node_buffer:
            nodes_by_label[label].append(props)
        flushed_total = 0
        skipped_total = 0
        for label, props_list in nodes_by_label.items():
            if not props_list:
                continue
            id_key = NODE_UNIQUE_CONSTRAINTS.get(label)
            if not id_key:
                logger.warning(ls.MG_NO_CONSTRAINT.format(label=label))
                skipped_total += len(props_list)
                continue

            batch_rows: list[NodeBatchRow] = []
            for props in props_list:
                if id_key not in props:
                    logger.warning(
                        ls.MG_MISSING_PROP.format(label=label, key=id_key, props=props)
                    )
                    skipped_total += 1
                    continue
                row_props: PropertyDict = {
                    k: v for k, v in props.items() if k != id_key
                }
                batch_rows.append(NodeBatchRow(id=props[id_key], props=row_props))

            if not batch_rows:
                continue

            flushed_total += len(batch_rows)

            query = build_merge_node_query(label, id_key)
            self._execute_batch(query, batch_rows)
        logger.info(
            ls.MG_NODES_FLUSHED.format(flushed=flushed_total, total=buffer_size)
        )
        if skipped_total:
            logger.info(ls.MG_NODES_SKIPPED.format(count=skipped_total))
        self.node_buffer.clear()

    def flush_relationships(self) -> None:
        if not self.relationship_buffer:
            return

        rels_by_pattern: defaultdict[
            tuple[str, str, str, str, str], list[RelBatchRow]
        ] = defaultdict(list)
        for from_node, rel_type, to_node, props in self.relationship_buffer:
            pattern = (from_node[0], from_node[1], rel_type, to_node[0], to_node[1])
            rels_by_pattern[pattern].append(
                RelBatchRow(from_val=from_node[2], to_val=to_node[2], props=props or {})
            )

        total_attempted = 0
        total_successful = 0

        for pattern, params_list in rels_by_pattern.items():
            from_label, from_key, rel_type, to_label, to_key = pattern
            has_props = any(p[KEY_PROPS] for p in params_list)
            query = build_merge_relationship_query(
                from_label, from_key, rel_type, to_label, to_key, has_props
            )

            total_attempted += len(params_list)
            results = self._execute_batch_with_return(query, params_list)
            batch_successful = 0
            for r in results:
                created = r.get(KEY_CREATED, 0)
                if isinstance(created, int):
                    batch_successful += created
            total_successful += batch_successful

            if rel_type == REL_TYPE_CALLS:
                failed = len(params_list) - batch_successful
                if failed > 0:
                    logger.warning(ls.MG_CALLS_FAILED.format(count=failed))
                    for i, sample in enumerate(params_list[:3]):
                        logger.warning(
                            ls.MG_CALLS_SAMPLE.format(
                                index=i + 1,
                                from_label=from_label,
                                from_val=sample[KEY_FROM_VAL],
                                to_label=to_label,
                                to_val=sample[KEY_TO_VAL],
                            )
                        )

        logger.info(
            ls.MG_RELS_FLUSHED.format(
                total=len(self.relationship_buffer),
                success=total_successful,
                failed=total_attempted - total_successful,
            )
        )
        self.relationship_buffer.clear()

    def flush_all(self) -> None:
        logger.info(ls.MG_FLUSH_START)
        self.flush_nodes()
        self.flush_relationships()
        logger.info(ls.MG_FLUSH_COMPLETE)

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        logger.debug(ls.MG_FETCH_QUERY.format(query=query, params=params))
        return self._execute_query(query, params)

    def execute_write(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> None:
        logger.debug(ls.MG_WRITE_QUERY.format(query=query, params=params))
        self._execute_query(query, params)

    def export_graph_to_dict(self) -> GraphData:
        logger.info(ls.MG_EXPORTING)

        nodes_data = self.fetch_all(CYPHER_EXPORT_NODES)
        relationships_data = self.fetch_all(CYPHER_EXPORT_RELATIONSHIPS)

        metadata = GraphMetadata(
            total_nodes=len(nodes_data),
            total_relationships=len(relationships_data),
            exported_at=self._get_current_timestamp(),
        )

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
