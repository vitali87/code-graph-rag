from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Configure anyio to only use asyncio backend."""
    return str(request.param)


@pytest.fixture
def temp_test_repo(tmp_path: Path) -> Path:
    """Create a temporary test repository with sample code."""
    sample_file = tmp_path / "sample.py"
    sample_file.write_text(
        '''def hello_world():
    """Say hello to the world."""
    print("Hello, World!")

class Calculator:
    """Simple calculator class."""

    def add(self, a: int, b: int) -> int:
        """Add two numbers."""
        return a + b
''',
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def mcp_registry(temp_test_repo: Path) -> MCPToolsRegistry:
    """Create MCP tools registry with minimal mocks."""
    mock_ingestor = MagicMock()
    mock_cypher_gen = MagicMock()

    async def mock_generate(query: str) -> str:
        return "MATCH (n) RETURN n"

    mock_cypher_gen.generate = mock_generate

    return MCPToolsRegistry(
        project_root=str(temp_test_repo),
        ingestor=mock_ingestor,
        cypher_gen=mock_cypher_gen,
    )


class TestMCPToolsIntegration:
    """Integration tests that call real Tool instances (no function mocking)."""

    async def test_query_code_graph_works(self, mcp_registry: MCPToolsRegistry) -> None:
        """Verify query_code_graph tool works without mocking."""
        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {"name": "func1"},
            {"name": "func2"},
        ]

        result = await mcp_registry.query_code_graph("find all functions")

        assert "error" not in result or result.get("error") is None
        assert "results" in result
        assert len(result["results"]) == 2

    async def test_read_file_works(self, mcp_registry: MCPToolsRegistry) -> None:
        """Verify read_file tool works without mocking."""
        result = await mcp_registry.read_file("sample.py")

        assert "Error" not in result
        assert "def hello_world()" in result
        assert "class Calculator:" in result

    async def test_get_code_snippet_actual_behavior(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test get_code_snippet works correctly after the fix."""
        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {
                "name": "hello_world",
                "start": 1,
                "end": 3,
                "path": "sample.py",
                "docstring": "Say hello to the world.",
            }
        ]

        result = await mcp_registry.get_code_snippet("sample.hello_world")

        assert result["found"] is True
        assert "def hello_world()" in result["source_code"]
        assert result["line_start"] == 1
        assert result["line_end"] == 3

    async def test_list_directory_works(self, mcp_registry: MCPToolsRegistry) -> None:
        """Verify list_directory tool works without mocking."""
        result = await mcp_registry.list_directory(".")

        assert "Error" not in result
        assert "sample.py" in result


class TestToolConsistency:
    """Tests that verify all tools follow consistent patterns."""

    async def test_all_service_classes_are_initialized(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Verify all service classes are properly initialized."""
        assert mcp_registry.code_retriever is not None
        assert mcp_registry.file_editor is not None
        assert mcp_registry.file_reader is not None
        assert mcp_registry.file_writer is not None
        assert mcp_registry.directory_lister is not None

    async def test_all_tools_are_registered(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Verify all expected tools are registered."""
        expected_tools = {
            "index_repository",
            "query_code_graph",
            "get_code_snippet",
            "surgical_replace_code",
            "read_file",
            "write_file",
            "list_directory",
        }

        registered_tools = set(mcp_registry.list_tool_names())
        assert registered_tools == expected_tools
