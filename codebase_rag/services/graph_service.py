from __future__ import annotations

import threading
import types
from collections import defaultdict
from collections.abc import Generator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime

import mgclient  # ty: ignore[unresolved-import]
from loguru import logger

from codebase_rag.config import settings
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
    build_create_node_query,
    build_create_relationship_query,
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
    __slots__ = (
        "_conn_lock",
        "_executor",
        "_host",
        "_port",
        "_username",
        "_password",
        "_use_merge",
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
    ):
        self._host = host
        self._port = port
        self._username = username.strip() if username and username.strip() else None
        self._password = password.strip() if password and password.strip() else None
        if (self._username is None) != (self._password is None):
            raise ValueError(ex.AUTH_INCOMPLETE)
        if batch_size < 1:
            raise ValueError(ex.BATCH_SIZE)
        self.batch_size = batch_size
        self._use_merge = use_merge
        self._conn_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self.conn: mgclient.Connection | None = None
        self.node_buffer: list[tuple[str, dict[str, PropertyValue]]] = []
        self._rel_count = 0
        self._rel_groups: defaultdict[
            tuple[str, str, str, str, str], list[RelBatchRow]
        ] = defaultdict(list)

    def __enter__(self) -> MemgraphIngestor:
        logger.info(ls.MG_CONNECTING.format(host=self._host, port=self._port))
        if self._username is not None:
            self.conn = mgclient.connect(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
            )
        else:
            self.conn = mgclient.connect(host=self._host, port=self._port)
        self.conn.autocommit = True
        self._executor = ThreadPoolExecutor(max_workers=settings.FLUSH_THREAD_POOL_SIZE)
        logger.info(ls.MG_CONNECTED)
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        try:
            if exc_type:
                logger.exception(ls.MG_EXCEPTION.format(error=exc_val))
                # (H) Best-effort flush: attempt to persist buffered nodes/relationships
                # (H) even when an exception occurred. Catching broad Exception so a
                # (H) secondary flush failure never masks the original exception.
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
                logger.info(ls.MG_DISCONNECTED)

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

    def _create_connection(self) -> mgclient.Connection:
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

    def _execute_batch_on(
        self,
        conn: mgclient.Connection,
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
        conn: mgclient.Connection,
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
        conn: mgclient.Connection | None = None,
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
        if conn is None:
            with self._conn_lock:
                self._execute_batch_on(target_conn, query, batch_rows)
        else:
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

        first_error: Exception | None = None

        if self._executor and len(nodes_by_label) > 1:
            logger.info(
                ls.MG_PARALLEL_FLUSH_NODES.format(
                    count=len(nodes_by_label),
                    workers=settings.FLUSH_THREAD_POOL_SIZE,
                )
            )
            futures = {
                self._executor.submit(
                    self._flush_node_group_with_own_conn, label, props_list
                ): label
                for label, props_list in nodes_by_label.items()
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    flushed, skipped = future.result()
                    flushed_total += flushed
                    skipped_total += skipped
                except Exception as e:
                    logger.error(
                        ls.MG_PARALLEL_LABEL_ERROR.format(label=label, error=e)
                    )
                    if first_error is None:
                        first_error = e
        else:
            for label, props_list in nodes_by_label.items():
                try:
                    flushed, skipped = self._flush_node_label_group(label, props_list)
                    flushed_total += flushed
                    skipped_total += skipped
                except Exception as e:
                    logger.error(
                        ls.MG_PARALLEL_LABEL_ERROR.format(label=label, error=e)
                    )
                    if first_error is None:
                        first_error = e

        logger.info(
            ls.MG_NODES_FLUSHED.format(flushed=flushed_total, total=buffer_size)
        )
        if skipped_total:
            logger.info(ls.MG_NODES_SKIPPED.format(count=skipped_total))
        self.node_buffer.clear()

        if first_error is not None:
            raise first_error

    def _flush_rel_pattern_group(
        self,
        pattern: tuple[str, str, str, str, str],
        params_list: list[RelBatchRow],
        conn: mgclient.Connection | None = None,
    ) -> tuple[int, int]:
        from_label, from_key, rel_type, to_label, to_key = pattern
        build_rel_query = (
            build_merge_relationship_query
            if self._use_merge
            else build_create_relationship_query
        )
        has_props = any(p[KEY_PROPS] for p in params_list)
        query = build_rel_query(
            from_label, from_key, rel_type, to_label, to_key, has_props
        )

        target_conn = conn or self.conn
        if not target_conn:
            logger.warning(ls.MG_NO_CONN_RELS.format(pattern=pattern))
            return len(params_list), 0
        if conn is None:
            with self._conn_lock:
                results = self._execute_batch_with_return_on(
                    target_conn, query, params_list
                )
        else:
            results = self._execute_batch_with_return_on(
                target_conn, query, params_list
            )
        batch_successful = 0
        for r in results:
            created = r.get(KEY_CREATED, 0)
            if isinstance(created, int):
                batch_successful += created

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

        return len(params_list), batch_successful

    def flush_relationships(self) -> None:
        if not self._rel_count:
            return

        total_attempted = 0
        total_successful = 0
        first_error: Exception | None = None

        if self._executor and len(self._rel_groups) > 1:
            logger.info(
                ls.MG_PARALLEL_FLUSH_RELS.format(
                    count=len(self._rel_groups),
                    workers=settings.FLUSH_THREAD_POOL_SIZE,
                )
            )
            futures = {
                self._executor.submit(
                    self._flush_rel_group_with_own_conn, pattern, params_list
                ): pattern
                for pattern, params_list in self._rel_groups.items()
            }
            for future in as_completed(futures):
                pattern = futures[future]
                try:
                    attempted, successful = future.result()
                    total_attempted += attempted
                    total_successful += successful
                except Exception as e:
                    logger.error(
                        ls.MG_PARALLEL_REL_ERROR.format(pattern=pattern, error=e)
                    )
                    if first_error is None:
                        first_error = e
        else:
            for pattern, params_list in self._rel_groups.items():
                try:
                    attempted, successful = self._flush_rel_pattern_group(
                        pattern, params_list
                    )
                    total_attempted += attempted
                    total_successful += successful
                except Exception as e:
                    logger.error(
                        ls.MG_PARALLEL_REL_ERROR.format(pattern=pattern, error=e)
                    )
                    if first_error is None:
                        first_error = e

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

    def flush_all(self) -> None:
        logger.info(ls.MG_FLUSH_START)
        self.flush_nodes()
        self.flush_relationships()
        logger.info(ls.MG_FLUSH_COMPLETE)

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        logger.debug(ls.MG_FETCH_QUERY, query=query, params=params)
        return self._execute_query(query, params)

    def execute_write(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> None:
        logger.debug(ls.MG_WRITE_QUERY, query=query, params=params)
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
