from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


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

    async def test_all_tools_have_consistent_takes_ctx(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Verify all tools have consistent takes_ctx settings."""
        tools = {
            "query": mcp_registry._query_tool,
            "code": mcp_registry._code_tool,
            "editor": mcp_registry._file_editor_tool,
            "reader": mcp_registry._file_reader_tool,
            "writer": mcp_registry._file_writer_tool,
            "lister": mcp_registry._directory_lister_tool,
        }

        takes_ctx_values = {name: tool.takes_ctx for name, tool in tools.items()}

        assert takes_ctx_values == {
            "query": False,
            "code": False,
            "editor": False,
            "reader": False,
            "writer": False,
            "lister": False,
        }

        assert all(not takes_ctx for takes_ctx in takes_ctx_values.values())
