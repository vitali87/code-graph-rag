from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.constants import NODE_UNIQUE_CONSTRAINTS
from codebase_rag.cypher_queries import build_project_name_indexes, wrap_with_unwind
from codebase_rag.services.graph_service import MemgraphIngestor


class TestMemgraphIngestorInit:
    def test_init_sets_host_and_port(self) -> None:
        ingestor = MemgraphIngestor(host="testhost", port=1234)

        assert ingestor._host == "testhost"
        assert ingestor._port == 1234

    def test_init_sets_default_batch_size(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        assert ingestor.batch_size == 1000

    def test_init_sets_custom_batch_size(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=500)

        assert ingestor.batch_size == 500

    def test_init_raises_for_zero_batch_size(self) -> None:
        with pytest.raises(ValueError, match="batch_size must be a positive integer"):
            MemgraphIngestor(host="localhost", port=7687, batch_size=0)

    def test_init_raises_for_negative_batch_size(self) -> None:
        with pytest.raises(ValueError, match="batch_size must be a positive integer"):
            MemgraphIngestor(host="localhost", port=7687, batch_size=-1)

    def test_init_creates_empty_buffers(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        assert ingestor.node_buffer == []
        assert ingestor.relationship_buffer == []

    def test_init_conn_is_none(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        assert ingestor.conn is None


class TestContextManager:
    def test_enter_connects_to_memgraph(self) -> None:
        with patch("codebase_rag.services.graph_service.mgclient") as mock_mgclient:
            mock_conn = MagicMock()
            mock_mgclient.connect.return_value = mock_conn

            ingestor = MemgraphIngestor(host="testhost", port=1234)
            result = ingestor.__enter__()

            mock_mgclient.connect.assert_called_once_with(host="testhost", port=1234)
            assert ingestor.conn == mock_conn
            assert mock_conn.autocommit is True
            assert result is ingestor

    def test_exit_flushes_and_closes_connection(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        ingestor.conn = mock_conn

        with patch.object(ingestor, "flush_all") as mock_flush:
            ingestor.__exit__(None, None, None)

            mock_flush.assert_called_once()
            mock_conn.close.assert_called_once()

    def test_exit_logs_error_on_exception(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        ingestor.conn = mock_conn

        with patch.object(ingestor, "flush_all"):
            ingestor.__exit__(ValueError, ValueError("test error"), None)

            mock_conn.close.assert_called_once()

    def test_exit_handles_none_connection(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        ingestor.conn = None

        with patch.object(ingestor, "flush_all"):
            ingestor.__exit__(None, None, None)


class TestCursorToResults:
    def test_returns_empty_list_when_no_description(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_cursor = MagicMock()
        mock_cursor.description = None

        result = ingestor._cursor_to_results(mock_cursor)

        assert result == []

    def test_converts_rows_to_dicts(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_cursor = MagicMock()

        col1 = MagicMock()
        col1.name = "id"
        col2 = MagicMock()
        col2.name = "name"
        mock_cursor.description = [col1, col2]
        mock_cursor.fetchall.return_value = [(1, "test"), (2, "other")]

        result = ingestor._cursor_to_results(mock_cursor)

        assert result == [{"id": 1, "name": "test"}, {"id": 2, "name": "other"}]

    def test_handles_single_row(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_cursor = MagicMock()

        col = MagicMock()
        col.name = "count"
        mock_cursor.description = [col]
        mock_cursor.fetchall.return_value = [(42,)]

        result = ingestor._cursor_to_results(mock_cursor)

        assert result == [{"count": 42}]

    def test_handles_empty_result_set(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_cursor = MagicMock()

        col = MagicMock()
        col.name = "value"
        mock_cursor.description = [col]
        mock_cursor.fetchall.return_value = []

        result = ingestor._cursor_to_results(mock_cursor)

        assert result == []


class TestExecuteQuery:
    def test_raises_when_not_connected(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        ingestor.conn = None

        with pytest.raises(ConnectionError, match="Not connected to Memgraph"):
            ingestor._execute_query("MATCH (n) RETURN n")

    def test_executes_query_and_returns_results(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        col = MagicMock()
        col.name = "n"
        mock_cursor.description = [col]
        mock_cursor.fetchall.return_value = [("node1",), ("node2",)]
        ingestor.conn = mock_conn

        result = ingestor._execute_query("MATCH (n) RETURN n")

        mock_cursor.execute.assert_called_once_with("MATCH (n) RETURN n", {})
        mock_cursor.close.assert_called_once()
        assert result == [{"n": "node1"}, {"n": "node2"}]

    def test_passes_params_to_query(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.description = None
        ingestor.conn = mock_conn

        ingestor._execute_query("MATCH (n {id: $id}) RETURN n", {"id": 123})

        mock_cursor.execute.assert_called_once_with(
            "MATCH (n {id: $id}) RETURN n", {"id": 123}
        )

    def test_closes_cursor_on_exception(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = RuntimeError("Database error")
        ingestor.conn = mock_conn

        with pytest.raises(RuntimeError):
            ingestor._execute_query("INVALID QUERY")

        mock_cursor.close.assert_called_once()

    def test_suppresses_already_exists_errors_in_logs(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = RuntimeError("Constraint already exists")
        ingestor.conn = mock_conn

        with pytest.raises(RuntimeError):
            ingestor._execute_query("CREATE CONSTRAINT")


class TestExecuteBatch:
    def test_returns_early_when_not_connected(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        ingestor.conn = None

        ingestor._execute_batch("MERGE (n:Test)", [{"id": 1}])

    def test_returns_early_when_params_empty(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        ingestor.conn = mock_conn

        ingestor._execute_batch("MERGE (n:Test)", [])

        mock_conn.cursor.assert_not_called()

    def test_wraps_query_with_unwind(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        ingestor._execute_batch("MERGE (n:Test {id: row.id})", [{"id": 1}, {"id": 2}])

        call_args = mock_cursor.execute.call_args[0]
        assert call_args[0] == wrap_with_unwind("MERGE (n:Test {id: row.id})")
        assert call_args[1] == {"batch": [{"id": 1}, {"id": 2}]}

    def test_closes_cursor_on_success(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        ingestor._execute_batch("MERGE (n:Test)", [{"id": 1}])

        mock_cursor.close.assert_called_once()


class TestCleanDatabase:
    def test_executes_delete_query(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        with patch.object(ingestor, "_execute_query") as mock_execute:
            ingestor.clean_database()

            mock_execute.assert_called_once_with("MATCH (n) DETACH DELETE n;")


class TestEnsureConstraints:
    def test_creates_constraint_for_each_node_type(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        executed_queries: list[str] = []

        def capture_query(query: str) -> None:
            executed_queries.append(query)

        with patch.object(ingestor, "_execute_query", side_effect=capture_query):
            ingestor.ensure_constraints()

        for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
            expected = f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"
            assert expected in executed_queries

    def test_continues_on_constraint_error(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        call_count = 0

        def fail_then_succeed(query: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Constraint already exists")

        with patch.object(ingestor, "_execute_query", side_effect=fail_then_succeed):
            ingestor.ensure_constraints()

        expected_queries = len(NODE_UNIQUE_CONSTRAINTS) * 2 + len(
            build_project_name_indexes()
        )
        assert call_count == expected_queries


class TestFlushNodesEdgeCases:
    def test_skips_nodes_with_unknown_label(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        ingestor.node_buffer.append(("UnknownLabel", {"some_prop": "value"}))

        ingestor.flush_nodes()

        mock_cursor.execute.assert_not_called()
        assert ingestor.node_buffer == []

    def test_skips_nodes_missing_id_property(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        ingestor.node_buffer.append(("File", {"name": "test.txt"}))

        ingestor.flush_nodes()

        mock_cursor.execute.assert_not_called()
        assert ingestor.node_buffer == []

    def test_processes_valid_nodes_and_skips_invalid(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        ingestor.node_buffer.append(("File", {"path": "/valid.txt", "name": "valid"}))
        ingestor.node_buffer.append(("File", {"name": "missing_path"}))
        ingestor.node_buffer.append(("UnknownLabel", {"id": "unknown"}))

        ingestor.flush_nodes()

        mock_cursor.execute.assert_called_once()
        batch = mock_cursor.execute.call_args[0][1]["batch"]
        assert len(batch) == 1
        assert batch[0]["id"] == "/valid.txt"

    def test_handles_empty_buffer(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        ingestor.conn = mock_conn

        ingestor.flush_nodes()

        mock_conn.cursor.assert_not_called()


class TestExportGraphToDict:
    def test_returns_graph_data_structure(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        mock_cursor.description = [
            MagicMock(name="node_id"),
            MagicMock(name="labels"),
            MagicMock(name="properties"),
        ]
        mock_cursor.description[0].name = "node_id"
        mock_cursor.description[1].name = "labels"
        mock_cursor.description[2].name = "properties"
        mock_cursor.fetchall.return_value = []

        result = ingestor.export_graph_to_dict()

        assert "nodes" in result
        assert "relationships" in result
        assert "metadata" in result
        assert "total_nodes" in result["metadata"]
        assert "total_relationships" in result["metadata"]
        assert "exported_at" in result["metadata"]

    def test_counts_nodes_and_relationships(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        call_count = 0

        def mock_fetch_all(query: str, params: dict | None = None) -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"node_id": 1}, {"node_id": 2}, {"node_id": 3}]
            return [{"from_id": 1, "to_id": 2}]

        with patch.object(ingestor, "fetch_all", side_effect=mock_fetch_all):
            result = ingestor.export_graph_to_dict()

        assert result["metadata"]["total_nodes"] == 3
        assert result["metadata"]["total_relationships"] == 1


class TestFlushAll:
    def test_calls_flush_nodes_and_flush_relationships(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        with (
            patch.object(ingestor, "flush_nodes") as mock_nodes,
            patch.object(ingestor, "flush_relationships") as mock_rels,
        ):
            ingestor.flush_all()

            mock_nodes.assert_called_once()
            mock_rels.assert_called_once()


class TestFetchAllAndExecuteWrite:
    def test_fetch_all_delegates_to_execute_query(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        with patch.object(
            ingestor, "_execute_query", return_value=[{"n": "result"}]
        ) as mock_exec:
            result = ingestor.fetch_all("MATCH (n) RETURN n", {"limit": 10})

            mock_exec.assert_called_once_with("MATCH (n) RETURN n", {"limit": 10})
            assert result == [{"n": "result"}]

    def test_execute_write_delegates_to_execute_query(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        with patch.object(ingestor, "_execute_query") as mock_exec:
            ingestor.execute_write("CREATE (n:Test)", {"name": "test"})

            mock_exec.assert_called_once_with("CREATE (n:Test)", {"name": "test"})


class TestGetCurrentTimestamp:
    def test_returns_iso_format_timestamp(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)

        result = ingestor._get_current_timestamp()

        assert "T" in result
        assert len(result) > 10
