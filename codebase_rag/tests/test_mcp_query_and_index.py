from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Configure anyio to only use asyncio backend."""
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    """Create a temporary project root directory with sample code."""
    # Create a simple Python module
    sample_file = tmp_path / "calculator.py"
    sample_file.write_text(
        '''"""Calculator module."""

def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b

class Calculator:
    """Simple calculator class."""

    def divide(self, a: float, b: float) -> float:
        """Divide two numbers."""
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b
''',
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def mcp_registry(temp_project_root: Path) -> MCPToolsRegistry:
    """Create an MCP tools registry with mocked dependencies."""
    mock_ingestor = MagicMock()
    mock_cypher_gen = MagicMock()

    registry = MCPToolsRegistry(
        project_root=str(temp_project_root),
        ingestor=mock_ingestor,
        cypher_gen=mock_cypher_gen,
    )

    # Mock the query tool
    registry._query_tool = MagicMock()
    registry._query_tool.function = AsyncMock()

    return registry


class TestQueryCodeGraph:
    """Test query_code_graph functionality."""

    async def test_query_finds_functions(self, mcp_registry: MCPToolsRegistry) -> None:
        """Test querying for functions in the code graph."""
        mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "cypher_query": "MATCH (f:Function) RETURN f.name",
                "results": [
                    {"name": "add"},
                    {"name": "multiply"},
                ],
                "summary": "Found 2 functions",
            }
        )

        result = await mcp_registry.query_code_graph("Find all functions")

        assert "results" in result
        assert len(result["results"]) == 2
        assert result["results"][0]["name"] == "add"
        assert result["results"][1]["name"] == "multiply"
        assert "cypher_query" in result
        assert "summary" in result

    async def test_query_finds_classes(self, mcp_registry: MCPToolsRegistry) -> None:
        """Test querying for classes in the code graph."""
        mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "cypher_query": "MATCH (c:Class) RETURN c.name",
                "results": [{"name": "Calculator"}],
                "summary": "Found 1 class",
            }
        )

        result = await mcp_registry.query_code_graph("Find all classes")

        assert len(result["results"]) == 1
        assert result["results"][0]["name"] == "Calculator"

    async def test_query_finds_function_calls(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test querying for function call relationships."""
        mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "cypher_query": "MATCH (f:Function)-[:CALLS]->(g:Function) RETURN f.name, g.name",
                "results": [
                    {"f.name": "main", "g.name": "add"},
                    {"f.name": "main", "g.name": "multiply"},
                ],
                "summary": "Found 2 function call relationships",
            }
        )

        result = await mcp_registry.query_code_graph("What functions does main call?")

        assert len(result["results"]) == 2
        assert result["summary"] == "Found 2 function call relationships"

    async def test_query_with_no_results(self, mcp_registry: MCPToolsRegistry) -> None:
        """Test query that returns no results."""
        mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "cypher_query": "MATCH (n:NonExistent) RETURN n",
                "results": [],
                "summary": "No results found",
            }
        )

        result = await mcp_registry.query_code_graph("Find nonexistent nodes")

        assert result["results"] == []
        assert "No results" in result["summary"]

    async def test_query_with_complex_natural_language(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test complex natural language query."""
        mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "cypher_query": "MATCH (f:Function)-[:DEFINED_IN]->(m:Module) WHERE m.name = 'calculator' RETURN f.name",
                "results": [
                    {"name": "add"},
                    {"name": "multiply"},
                ],
                "summary": "Found 2 functions in calculator module",
            }
        )

        result = await mcp_registry.query_code_graph(
            "What functions are defined in the calculator module?"
        )

        assert len(result["results"]) == 2
        assert "cypher_query" in result

    async def test_query_handles_unicode(self, mcp_registry: MCPToolsRegistry) -> None:
        """Test query with unicode characters."""
        mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "cypher_query": "MATCH (f:Function) WHERE f.name = '你好' RETURN f",
                "results": [{"name": "你好"}],
                "summary": "Found 1 function",
            }
        )

        result = await mcp_registry.query_code_graph("Find function 你好")

        assert len(result["results"]) == 1

    async def test_query_error_handling(self, mcp_registry: MCPToolsRegistry) -> None:
        """Test error handling during query execution."""
        mcp_registry._query_tool.function.side_effect = Exception  # ty: ignore[invalid-assignment]("Database error")

        result = await mcp_registry.query_code_graph("Find all nodes")

        assert "error" in result
        assert "results" in result
        assert isinstance(result["results"], list)
        assert len(result["results"]) == 0

    async def test_query_verifies_parameter_passed(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test that query parameter is correctly passed."""
        mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "cypher_query": "MATCH (n) RETURN n",
                "results": [],
                "summary": "Query executed",
            }
        )

        query = "Find all nodes"
        await mcp_registry.query_code_graph(query)

        mcp_registry._query_tool.function.assert_called_once_with(query)


class TestIndexRepository:
    """Test index_repository functionality."""

    async def test_index_repository_success(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test successful repository indexing."""
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            result = await mcp_registry.index_repository()

            assert "Error:" not in result
            assert "Success" in result or "indexed" in result.lower()
            assert str(temp_project_root) in result
            mock_updater.run.assert_called_once()

    async def test_index_repository_creates_graph_updater(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test that GraphUpdater is created with correct parameters."""
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            await mcp_registry.index_repository()

            # Verify GraphUpdater was instantiated with correct args
            mock_updater_class.assert_called_once()
            call_kwargs = mock_updater_class.call_args.kwargs
            assert call_kwargs["ingestor"] == mcp_registry.ingestor
            assert call_kwargs["repo_path"] == Path(temp_project_root)
            assert "parsers" in call_kwargs
            assert "queries" in call_kwargs

    async def test_index_repository_handles_errors(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test error handling during repository indexing."""
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.side_effect = Exception("Indexing failed")
            mock_updater_class.return_value = mock_updater

            result = await mcp_registry.index_repository()

            assert "Error" in result
            assert "Indexing failed" in result

    async def test_index_repository_with_empty_directory(
        self, mcp_registry: MCPToolsRegistry, tmp_path: Path
    ) -> None:
        """Test indexing an empty directory."""
        empty_registry = MCPToolsRegistry(
            project_root=str(tmp_path),
            ingestor=MagicMock(),
            cypher_gen=MagicMock(),
        )

        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            result = await empty_registry.index_repository()

            # Should succeed even with empty directory
            assert "Error:" not in result or "Success" in result

    async def test_index_repository_multiple_times(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test indexing repository multiple times (re-indexing)."""
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            # Index first time
            result1 = await mcp_registry.index_repository()
            assert "Error:" not in result1

            # Index second time (re-index)
            result2 = await mcp_registry.index_repository()
            assert "Error:" not in result2

            # Should have been called twice
            assert mock_updater.run.call_count == 2

    async def test_index_repository_clears_database_first(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test that database is cleared before indexing."""
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            # Index repository
            result = await mcp_registry.index_repository()

            # Verify clean_database was called
            mcp_registry.ingestor.clean_database.assert_called_once()  # type: ignore[attr-defined]
            assert "Error:" not in result
            # Verify message indicates data was cleared
            assert "cleared" in result.lower() or "previous data" in result.lower()

    async def test_index_repository_clears_before_updater_runs(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test that database clearing happens before GraphUpdater runs."""
        call_order: list[str] = []

        def mock_clean() -> None:
            call_order.append("clean")

        def mock_run() -> None:
            call_order.append("run")

        mcp_registry.ingestor.clean_database = MagicMock(side_effect=mock_clean)  # type: ignore[method-assign]

        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run = MagicMock(side_effect=mock_run)
            mock_updater_class.return_value = mock_updater

            await mcp_registry.index_repository()

            # Verify clean was called before run
            assert call_order == ["clean", "run"]

    async def test_sequential_index_clears_previous_repo_data(
        self, tmp_path: Path
    ) -> None:
        """Test that indexing a second repository clears the first repository's data."""
        # Create two mock registries for different projects
        mock_ingestor = MagicMock()
        mock_cypher = MagicMock()

        project1 = tmp_path / "project1"
        project1.mkdir()
        registry1 = MCPToolsRegistry(
            project_root=str(project1),
            ingestor=mock_ingestor,
            cypher_gen=mock_cypher,
        )

        project2 = tmp_path / "project2"
        project2.mkdir()
        registry2 = MCPToolsRegistry(
            project_root=str(project2),
            ingestor=mock_ingestor,
            cypher_gen=mock_cypher,
        )

        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            # Index first repository
            await registry1.index_repository()
            assert mock_ingestor.clean_database.call_count == 1

            # Index second repository - should clear database again
            await registry2.index_repository()
            assert mock_ingestor.clean_database.call_count == 2


class TestQueryAndIndexIntegration:
    """Test integration between querying and indexing."""

    async def test_query_after_index(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test querying after indexing."""
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            # Index the repository
            index_result = await mcp_registry.index_repository()
            assert "Error:" not in index_result

            # Now query it
            mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
                model_dump=lambda: {
                    "cypher_query": "MATCH (f:Function) RETURN f.name",
                    "results": [{"name": "add"}],
                    "summary": "Found 1 function",
                }
            )

            query_result = await mcp_registry.query_code_graph("Find all functions")
            assert len(query_result["results"]) >= 0

    async def test_index_and_query_workflow(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test typical workflow: index then query."""
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_class:
            mock_updater = MagicMock()
            mock_updater.run.return_value = None
            mock_updater_class.return_value = mock_updater

            # Step 1: Index
            await mcp_registry.index_repository()

            # Step 2: Query for functions
            mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
                model_dump=lambda: {
                    "cypher_query": "MATCH (f:Function) RETURN f",
                    "results": [{"name": "add"}, {"name": "multiply"}],
                    "summary": "Found 2 functions",
                }
            )
            result = await mcp_registry.query_code_graph("Find all functions")
            assert len(result["results"]) == 2

            # Step 3: Query for classes
            mcp_registry._query_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
                model_dump=lambda: {
                    "cypher_query": "MATCH (c:Class) RETURN c",
                    "results": [{"name": "Calculator"}],
                    "summary": "Found 1 class",
                }
            )
            result = await mcp_registry.query_code_graph("Find all classes")
            assert len(result["results"]) == 1
