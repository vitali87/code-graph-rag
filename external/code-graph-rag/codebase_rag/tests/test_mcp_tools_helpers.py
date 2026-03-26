from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs

_PATCH_DELETE = "codebase_rag.mcp.tools.delete_project_embeddings"


def _make_registry(mock_ingestor: MagicMock) -> MagicMock:
    from codebase_rag.mcp.tools import MCPToolsRegistry

    registry = MagicMock(spec=MCPToolsRegistry)
    registry.ingestor = mock_ingestor
    registry._get_project_node_ids = MCPToolsRegistry._get_project_node_ids.__get__(
        registry
    )
    registry._cleanup_project_embeddings = (
        MCPToolsRegistry._cleanup_project_embeddings.__get__(registry)
    )
    return registry


class TestGetProjectNodeIds:
    def test_returns_integer_ids(self) -> None:
        mock_ingestor = MagicMock()
        mock_ingestor.fetch_all.return_value = [
            {cs.KEY_NODE_ID: 1},
            {cs.KEY_NODE_ID: 2},
            {cs.KEY_NODE_ID: 3},
        ]
        registry = _make_registry(mock_ingestor)

        result = registry._get_project_node_ids("myproject")

        assert result == [1, 2, 3]
        mock_ingestor.fetch_all.assert_called_once_with(
            cs.CYPHER_QUERY_PROJECT_NODE_IDS,
            {cs.KEY_PROJECT_NAME: "myproject"},
        )

    def test_filters_non_integer_ids(self) -> None:
        mock_ingestor = MagicMock()
        mock_ingestor.fetch_all.return_value = [
            {cs.KEY_NODE_ID: 1},
            {cs.KEY_NODE_ID: "not_an_int"},
            {cs.KEY_NODE_ID: None},
            {cs.KEY_NODE_ID: 4},
        ]
        registry = _make_registry(mock_ingestor)

        result = registry._get_project_node_ids("proj")

        assert result == [1, 4]

    def test_returns_empty_when_no_rows(self) -> None:
        mock_ingestor = MagicMock()
        mock_ingestor.fetch_all.return_value = []
        registry = _make_registry(mock_ingestor)

        result = registry._get_project_node_ids("empty")

        assert result == []

    def test_skips_rows_missing_key(self) -> None:
        mock_ingestor = MagicMock()
        mock_ingestor.fetch_all.return_value = [
            {"other_key": 99},
            {cs.KEY_NODE_ID: 5},
        ]
        registry = _make_registry(mock_ingestor)

        result = registry._get_project_node_ids("proj")

        assert result == [5]


class TestCleanupProjectEmbeddings:
    def test_calls_delete_with_node_ids(self) -> None:
        mock_ingestor = MagicMock()
        mock_ingestor.fetch_all.return_value = [
            {cs.KEY_NODE_ID: 10},
            {cs.KEY_NODE_ID: 20},
        ]
        registry = _make_registry(mock_ingestor)

        with patch(_PATCH_DELETE) as mock_delete:
            registry._cleanup_project_embeddings("myproject")

        mock_delete.assert_called_once_with("myproject", [10, 20])

    def test_calls_delete_with_empty_list_when_no_nodes(self) -> None:
        mock_ingestor = MagicMock()
        mock_ingestor.fetch_all.return_value = []
        registry = _make_registry(mock_ingestor)

        with patch(_PATCH_DELETE) as mock_delete:
            registry._cleanup_project_embeddings("empty_proj")

        mock_delete.assert_called_once_with("empty_proj", [])
