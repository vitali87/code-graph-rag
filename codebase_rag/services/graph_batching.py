"""The graph ingestor's write batching: the node and relationship buffers,
and their flush, per label or pattern group, serially or on a worker pool
with a connection per group.

`GraphIngestor` (graph_service.py) composes this with its session and its
query helpers; the state read here is set by its `__init__`.
"""

import threading
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from functools import partial

from loguru import logger

from codebase_rag.config import settings
from codebase_rag.types_defs import ConnectionProtocol

from .. import constants as cs
from .. import logs as ls
from ..cypher_queries import (
    build_create_node_query,
    build_create_relationship_query,
    build_merge_node_query,
    build_merge_relationship_query,
    build_missing_rel_endpoints_query,
    wrap_with_unwind,
)
from ..embedded_graph.connection import EmbeddedConnection
from ..embedded_graph.native import NativeGraph, RelMerge
from ..types_defs import (
    BatchParams,
    BatchWrapper,
    NodeBatchRow,
    PropertyDict,
    PropertyValue,
    RelBatchRow,
    ResultRow,
)
from .graph_session import GraphSession, retry_transient


def _group_by_merge_keys(
    rel_type: str, params_list: list[RelBatchRow]
) -> dict[tuple[str, ...], list[RelBatchRow]]:
    # Bucket rows by which of the relationship's merge-key props they carry.
    candidate = cs.MERGE_KEY_PROPS_BY_REL.get(rel_type, ())
    by_keys: defaultdict[tuple[str, ...], list[RelBatchRow]] = defaultdict(list)
    for row in params_list:
        props = row[cs.KEY_PROPS] or {}
        by_keys[tuple(p for p in candidate if p in props)].append(row)
    return by_keys


def _native_graph(conn: ConnectionProtocol) -> NativeGraph | None:
    """The Rust extension's handle on an embedded connection's file, if any."""
    return conn.native if isinstance(conn, EmbeddedConnection) else None


def _created_count(results: Sequence[ResultRow]) -> int:
    total = 0
    for r in results:
        created = r.get(cs.KEY_CREATED, 0)
        if isinstance(created, int):
            total += created
    return total


def _missing_endpoints(row: ResultRow) -> str:
    return cs.REL_ENDPOINT_JOINER.join(
        side
        for side, key in (
            (cs.REL_ENDPOINT_SOURCE, cs.KEY_FROM_MISSING),
            (cs.REL_ENDPOINT_TARGET, cs.KEY_TO_MISSING),
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
    ][: cs.FAILED_REL_ROWS_SHOWN]
    for index, (row, missing) in enumerate(named, start=1):
        logger.warning(
            ls.MG_REL_FAILED_ROW.format(
                index=index,
                from_label=from_label,
                from_val=row.get(cs.KEY_FROM_VAL),
                to_label=to_label,
                to_val=row.get(cs.KEY_TO_VAL),
                missing=missing,
            )
        )
    if named and failed > len(named):
        logger.warning(ls.MG_RELS_FAILED_MORE.format(count=failed - len(named)))


class GraphBatching(GraphSession):
    """Buffered node and relationship writes, flushed in batches."""

    # Set by `GraphIngestor.__init__`, which says what each one holds.
    __slots__ = (
        "_executor",
        "_node_flush_lock",
        "_rel_count",
        "_rel_groups",
        "_use_merge",
        "batch_size",
        "node_buffer",
    )

    _use_merge: bool
    _node_flush_lock: threading.Lock
    _executor: ThreadPoolExecutor | None
    _rel_count: int
    _rel_groups: defaultdict[tuple[str, str, str, str, str], list[RelBatchRow]]
    batch_size: int
    node_buffer: list[tuple[str, dict[str, PropertyValue]]]

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
            retry_transient(
                partial(
                    cursor.execute,
                    wrap_with_unwind(query),
                    BatchWrapper(batch=params_list),
                )
            )
        except Exception as e:
            if cs.ERR_SUBSTR_ALREADY_EXISTS not in str(e).lower():
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
            retry_transient(
                partial(
                    cursor.execute,
                    wrap_with_unwind(query),
                    BatchWrapper(batch=params_list),
                )
            )
            return self._cursor_to_results(cursor)
        except Exception as e:
            logger.error(ls.MG_BATCH_ERROR.format(error=e))
            logger.error(ls.MG_CYPHER_QUERY.format(query=query))
            raise
        finally:
            if cursor:
                cursor.close()

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

        id_key = cs.NODE_UNIQUE_CONSTRAINTS.get(label)
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
            self._write_node_rows(target_conn, label, id_key, query, batch_rows)
        return len(batch_rows), skipped

    def _write_node_rows(
        self,
        conn: ConnectionProtocol,
        label: str,
        id_key: str,
        query: str,
        batch_rows: list[NodeBatchRow],
    ) -> None:
        # The Rust extension writes the same rows when it can (native.py).
        native = _native_graph(conn) if self._use_merge else None
        if native is None or not native.merge_nodes(label, id_key, batch_rows):
            self._execute_batch_on(conn, query, batch_rows)

    def _flush_node_group_with_own_conn(
        self,
        label: str,
        props_list: list[dict[str, PropertyValue]],
    ) -> tuple[int, int]:
        conn = self.open_connection()
        try:
            return self._flush_node_label_group(label, props_list, conn=conn)
        finally:
            conn.close()

    def _flush_rel_group_with_own_conn(
        self,
        pattern: tuple[str, str, str, str, str],
        params_list: list[RelBatchRow],
    ) -> int:
        conn = self.open_connection()
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
            except Exception as e:  # noqa: BLE001 -- collected per group and re-raised once the rest flushed
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
            except Exception as e:  # noqa: BLE001 -- collected per group and re-raised once the rest flushed
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
    ) -> int:
        """The number of rows written for one relationship pattern."""
        from_label, from_key, rel_type, to_label, to_key = pattern
        has_props = any(p[cs.KEY_PROPS] for p in params_list)
        if self._use_merge:
            by_keys = _group_by_merge_keys(rel_type, params_list)
            if len(by_keys) > 1:
                # Rows for the same endpoints may carry different distinguishing
                # props (issue #722); flush each merge-key signature on its own so
                # a prop absent from one row is not dropped from the key for the
                # rest, which would re-collapse the parallel provenance edges.
                # Pass `conn` through unchanged to preserve the lock semantics.
                return sum(
                    self._flush_rel_pattern_group(pattern, rows, conn=conn)
                    for rows in by_keys.values()
                )
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
            merge = RelMerge(merge_key_props, has_props)
        else:
            query = build_create_relationship_query(
                from_label, from_key, rel_type, to_label, to_key, has_props
            )
            merge = None
        return self._execute_rel_pattern_query(pattern, query, params_list, conn, merge)

    def _execute_rel_pattern_query(
        self,
        pattern: tuple[str, str, str, str, str],
        query: str,
        params_list: list[RelBatchRow],
        conn: ConnectionProtocol | None,
        merge: RelMerge | None = None,
    ) -> int:
        target_conn = conn or self.conn
        if not target_conn:
            logger.warning(ls.MG_NO_CONN_RELS.format(pattern=pattern))
            return 0
        lock = self._conn_lock if conn is None else nullcontext()
        indexed = [
            RelBatchRow(**row, row_index=index) for index, row in enumerate(params_list)
        ]
        with lock:
            # `merge` says what the MERGE query was built from, so the Rust
            # extension can write the same rows (native.py).
            native = _native_graph(target_conn) if merge is not None else None
            created = (
                native.merge_relationships(pattern, merge, indexed)
                if native is not None and merge is not None
                else None
            )
            if created is None:
                results = self._execute_batch_with_return_on(
                    target_conn, query, indexed
                )
                created = _created_count(results)
            batch_successful = created
            failed_rows = (
                self._missing_endpoint_rows(target_conn, pattern, params_list)
                if batch_successful < len(params_list)
                else []
            )

        _log_failed_relationships(
            pattern, len(params_list), batch_successful, failed_rows
        )

        return batch_successful

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
            from_label, from_key, to_label, to_key, cs.FAILED_REL_ROWS_SHOWN
        )
        try:
            return self._execute_batch_with_return_on(conn, query, params_list)
        except Exception as e:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(ls.MG_RELS_FAILED_LOOKUP.format(rel_type=rel_type, error=e))
            return []

    def flush_relationships(self) -> None:
        if not self._rel_count:
            return

        if self._executor and len(self._rel_groups) > 1:
            total_successful, first_error = self._flush_rel_groups_parallel(
                self._executor
            )
        else:
            total_successful, first_error = self._flush_rel_groups_serial()

        # Failed is what the total lacks, so the rows of a group whose write
        # raised (never attempted) count too, and the three numbers add up.
        logger.info(
            ls.MG_RELS_FLUSHED.format(
                total=self._rel_count,
                success=total_successful,
                failed=self._rel_count - total_successful,
            )
        )
        self._rel_count = 0
        self._rel_groups.clear()

        if first_error is not None:
            raise first_error

    def _flush_rel_groups_parallel(
        self, executor: ThreadPoolExecutor
    ) -> tuple[int, Exception | None]:
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
        total_successful = 0
        first_error: Exception | None = None
        for future in as_completed(futures):
            pattern = futures[future]
            try:
                successful = future.result()
            except Exception as e:  # noqa: BLE001 -- collected per group and re-raised once the rest flushed
                logger.error(ls.MG_REL_FLUSH_ERROR.format(pattern=pattern, error=e))
                if first_error is None:
                    first_error = e
                continue
            total_successful += successful
        return total_successful, first_error

    def _flush_rel_groups_serial(self) -> tuple[int, Exception | None]:
        total_successful = 0
        first_error: Exception | None = None
        for pattern, params_list in self._rel_groups.items():
            try:
                successful = self._flush_rel_pattern_group(pattern, params_list)
            except Exception as e:  # noqa: BLE001 -- collected per group and re-raised once the rest flushed
                logger.error(ls.MG_REL_FLUSH_ERROR.format(pattern=pattern, error=e))
                if first_error is None:
                    first_error = e
                continue
            total_successful += successful
        return total_successful, first_error

    def flush_all(self) -> None:
        logger.debug(ls.MG_FLUSH_START)
        self.flush_nodes()
        self.flush_relationships()
        logger.debug(ls.MG_FLUSH_COMPLETE)
