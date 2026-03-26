from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Configure anyio to only use asyncio backend."""
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    """Create a temporary project root directory."""
    return tmp_path


@pytest.fixture
def sample_file(temp_project_root: Path) -> Path:
    """Create a sample file with known content."""
    file_path = temp_project_root / "test_file.txt"
    content = "\n".join([f"Line {i}" for i in range(1, 101)])
    file_path.write_text(content, encoding="utf-8")
    return file_path


@pytest.fixture
def large_file(temp_project_root: Path) -> Path:
    """Create a large file to test memory efficiency."""
    file_path = temp_project_root / "large_file.txt"
    with open(file_path, "w", encoding="utf-8") as f:
        for i in range(1, 10001):
            f.write(f"This is line {i} with some content\n")
    return file_path


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

    registry._file_reader_tool = MagicMock()
    registry._file_reader_tool.function = AsyncMock()

    return registry


class TestReadFileWithoutPagination:
    """Test reading files without pagination parameters."""

    async def test_read_full_file(
        self, mcp_registry: MCPToolsRegistry, sample_file: Path
    ) -> None:
        expected_content = sample_file.read_text(encoding="utf-8")
        mock_func = mcp_registry._file_reader_tool.function
        assert isinstance(mock_func, AsyncMock)
        mock_func.return_value = expected_content

        result = await mcp_registry.read_file("test_file.txt")

        assert result == expected_content
        mock_func.assert_called_once_with(file_path="test_file.txt")


class TestReadFileWithPagination:
    """Test reading files with pagination parameters."""

    async def test_read_with_offset_only(
        self, mcp_registry: MCPToolsRegistry, sample_file: Path
    ) -> None:
        """Test reading from a specific offset to end of file."""
        result = await mcp_registry.read_file("test_file.txt", offset=10)

        lines = result.split("\n")
        assert lines[0].startswith("# Lines 11-")
        assert lines[1] == "Line 11"
        assert "Line 100" in result

    async def test_read_with_limit_only(
        self, mcp_registry: MCPToolsRegistry, sample_file: Path
    ) -> None:
        """Test reading only first N lines."""
        result = await mcp_registry.read_file("test_file.txt", limit=10)

        lines = result.split("\n")
        assert lines[0] == "# Lines 1-10 of 100"
        assert lines[1] == "Line 1"
        assert lines[10] == "Line 10"
        assert "Line 11" not in result

    async def test_read_with_offset_and_limit(
        self, mcp_registry: MCPToolsRegistry, sample_file: Path
    ) -> None:
        """Test reading a specific range of lines."""
        result = await mcp_registry.read_file("test_file.txt", offset=20, limit=10)

        lines = result.split("\n")
        assert lines[0] == "# Lines 21-30 of 100"
        assert lines[1] == "Line 21"
        assert lines[10] == "Line 30"
        assert "Line 20" not in result
        assert "Line 31" not in result

    async def test_read_offset_beyond_file_length(
        self, mcp_registry: MCPToolsRegistry, sample_file: Path
    ) -> None:
        """Test reading with offset beyond file length."""
        result = await mcp_registry.read_file("test_file.txt", offset=150)

        lines = result.split("\n")
        assert "of 100" in lines[0]
        assert lines[0] == "# Lines 151-150 of 100"

    async def test_read_zero_offset(
        self, mcp_registry: MCPToolsRegistry, sample_file: Path
    ) -> None:
        """Test reading with offset=0 (should read from beginning)."""
        result = await mcp_registry.read_file("test_file.txt", offset=0, limit=5)

        lines = result.split("\n")
        assert lines[0] == "# Lines 1-5 of 100"
        assert lines[1] == "Line 1"
        assert lines[5] == "Line 5"


class TestReadFileLargeFiles:
    """Test memory efficiency with large files."""

    async def test_read_middle_of_large_file(
        self, mcp_registry: MCPToolsRegistry, large_file: Path
    ) -> None:
        """Test reading from middle of large file doesn't load entire file."""
        result = await mcp_registry.read_file("large_file.txt", offset=5000, limit=10)

        lines = result.split("\n")
        assert lines[0] == "# Lines 5001-5010 of 10000"
        assert "This is line 5001" in lines[1]
        assert "This is line 5010" in lines[10]
        assert "line 1 " not in result
        assert "line 10000" not in result

    async def test_read_last_lines_of_large_file(
        self, mcp_registry: MCPToolsRegistry, large_file: Path
    ) -> None:
        """Test reading last few lines of large file."""
        result = await mcp_registry.read_file("large_file.txt", offset=9995, limit=10)

        lines = result.split("\n")
        assert "Lines 9996-10000" in lines[0]
        assert "This is line 9996" in lines[1]
        assert "This is line 10000" in result


class TestReadFileEdgeCases:
    """Test edge cases and error handling."""

    async def test_read_empty_file(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test reading an empty file."""
        empty_file = temp_project_root / "empty.txt"
        empty_file.write_text("", encoding="utf-8")

        result = await mcp_registry.read_file("empty.txt", offset=0, limit=10)

        lines = result.split("\n")
        assert lines[0] == "# Lines 1-0 of 0"

    async def test_read_nonexistent_file(self, mcp_registry: MCPToolsRegistry) -> None:
        """Test reading a file that doesn't exist."""
        result = await mcp_registry.read_file("nonexistent.txt", offset=0, limit=10)

        assert "Error:" in result

    async def test_read_single_line_file(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test reading a file with only one line."""
        single_line_file = temp_project_root / "single.txt"
        single_line_file.write_text("Only one line", encoding="utf-8")

        result = await mcp_registry.read_file("single.txt", offset=0, limit=10)

        lines = result.split("\n")
        assert lines[0] == "# Lines 1-1 of 1"
        assert lines[1] == "Only one line"

    async def test_read_file_with_unicode(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test reading a file with unicode characters."""
        unicode_file = temp_project_root / "unicode.txt"
        content = "\n".join(
            ["Hello 世界", "Привет мир", "مرحبا بالعالم", "🎉 Emoji line"]
        )
        unicode_file.write_text(content, encoding="utf-8")

        result = await mcp_registry.read_file("unicode.txt", offset=1, limit=2)

        assert "Привет мир" in result
        assert "مرحبا بالعالم" in result
        assert "Hello 世界" not in result
