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
def temp_project_root(tmp_path: Path) -> Path:
    """Create a temporary project root directory."""
    return tmp_path


@pytest.fixture
def sample_directory_structure(temp_project_root: Path) -> Path:
    """Create a sample directory structure for testing."""
    (temp_project_root / "file1.txt").write_text("content1", encoding="utf-8")
    (temp_project_root / "file2.py").write_text("content2", encoding="utf-8")

    (temp_project_root / "subdir1").mkdir()
    (temp_project_root / "subdir2").mkdir()

    nested = temp_project_root / "subdir1" / "nested"
    nested.mkdir(parents=True)
    (nested / "nested_file.txt").write_text("nested content", encoding="utf-8")

    return temp_project_root


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

    return registry


class TestListDirectoryBasic:
    """Test basic directory listing functionality."""

    async def test_list_root_directory(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test listing the root directory."""
        result = await mcp_registry.list_directory(".")

        assert "file1.txt" in result
        assert "file2.py" in result
        assert "subdir1" in result
        assert "subdir2" in result

        assert "nested_file.txt" not in result

    async def test_list_subdirectory(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test listing a subdirectory."""
        result = await mcp_registry.list_directory("subdir1")

        assert "nested" in result

        assert "file1.txt" not in result
        assert "file2.py" not in result

    async def test_list_nested_directory(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test listing a deeply nested directory."""
        result = await mcp_registry.list_directory("subdir1/nested")

        assert "nested_file.txt" in result

        assert "subdir2" not in result

    async def test_list_empty_directory(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test listing an empty directory."""
        empty_dir = temp_project_root / "empty"
        empty_dir.mkdir()

        result = await mcp_registry.list_directory("empty")

        assert "empty" in result.lower()


class TestListDirectoryEdgeCases:
    """Test edge cases and error handling."""

    async def test_list_nonexistent_directory(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test listing a directory that doesn't exist."""
        result = await mcp_registry.list_directory("nonexistent")

        assert "Error:" in result or "not a valid directory" in result

    async def test_list_file_instead_of_directory(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test listing a file path instead of directory."""
        result = await mcp_registry.list_directory("file1.txt")

        assert "not a valid directory" in result

    async def test_list_directory_with_special_characters(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test listing directory with special characters in names."""
        special_dir = temp_project_root / "special-dir_123"
        special_dir.mkdir()
        (special_dir / "file with spaces.txt").write_text("content", encoding="utf-8")
        (special_dir / "file@special#chars.py").write_text("content", encoding="utf-8")

        result = await mcp_registry.list_directory("special-dir_123")

        assert "file with spaces.txt" in result
        assert "file@special#chars.py" in result

    async def test_list_directory_with_hidden_files(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test listing directory with hidden files (dotfiles)."""
        hidden_dir = temp_project_root / "hidden"
        hidden_dir.mkdir()
        (hidden_dir / ".hidden_file").write_text("hidden", encoding="utf-8")
        (hidden_dir / "visible_file").write_text("visible", encoding="utf-8")

        result = await mcp_registry.list_directory("hidden")

        assert ".hidden_file" in result
        assert "visible_file" in result


class TestListDirectoryPathHandling:
    """Test various path formats and security."""

    async def test_list_with_relative_path(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test listing with relative path."""
        result = await mcp_registry.list_directory("./subdir1")

        assert "nested" in result

    async def test_list_with_absolute_path(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test listing with absolute path within project root."""
        abs_path = str(sample_directory_structure / "subdir1")
        result = await mcp_registry.list_directory(abs_path)

        assert "nested" in result

    async def test_list_prevents_directory_traversal(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test that directory traversal attacks are prevented."""
        result = await mcp_registry.list_directory("../../../etc")

        assert "Error:" in result or "denied" in result.lower()


class TestListDirectoryOutput:
    """Test the output format of directory listing."""

    async def test_output_is_newline_separated(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test that output is newline-separated list."""
        result = await mcp_registry.list_directory(".")

        lines = result.split("\n")
        assert len(lines) >= 4

    async def test_output_contains_only_names_not_paths(
        self, mcp_registry: MCPToolsRegistry, sample_directory_structure: Path
    ) -> None:
        """Test that output contains only names, not full paths."""
        result = await mcp_registry.list_directory("subdir1")

        assert "nested" in result
        assert str(sample_directory_structure / "subdir1" / "nested") not in result
