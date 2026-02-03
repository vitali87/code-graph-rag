import os
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


class TestWriteFileBasic:
    """Test basic file writing functionality."""

    async def test_write_new_file(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing a new file."""
        content = "Hello, World!"
        result = await mcp_registry.write_file("test.txt", content)

        assert "Error:" not in result

        file_path = temp_project_root / "test.txt"
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == content

    async def test_write_file_in_subdirectory(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing a file in a subdirectory (should create directory)."""
        content = "Nested content"
        result = await mcp_registry.write_file("subdir/nested/file.txt", content)

        assert "Error:" not in result

        file_path = temp_project_root / "subdir" / "nested" / "file.txt"
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == content

    async def test_overwrite_existing_file(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test overwriting an existing file."""
        file_path = temp_project_root / "existing.txt"
        file_path.write_text("Original content", encoding="utf-8")

        new_content = "New content"
        result = await mcp_registry.write_file("existing.txt", new_content)

        assert "Error:" not in result

        assert file_path.read_text(encoding="utf-8") == new_content

    async def test_write_empty_file(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing an empty file."""
        result = await mcp_registry.write_file("empty.txt", "")

        assert "Error:" not in result

        file_path = temp_project_root / "empty.txt"
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == ""


class TestWriteFileContent:
    """Test writing various content types."""

    async def test_write_multiline_content(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing multiline content."""
        content = "Line 1\nLine 2\nLine 3\n"
        result = await mcp_registry.write_file("multiline.txt", content)

        assert "Error:" not in result
        file_path = temp_project_root / "multiline.txt"
        assert file_path.read_text(encoding="utf-8") == content

    async def test_write_unicode_content(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing unicode content."""
        content = "Hello 世界\nПривет мир\nمرحبا بالعالم\n🎉 Emoji"
        result = await mcp_registry.write_file("unicode.txt", content)

        assert "Error:" not in result
        file_path = temp_project_root / "unicode.txt"
        assert file_path.read_text(encoding="utf-8") == content

    async def test_write_python_code(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing Python code."""
        content = '''def hello():
    """Say hello."""
    print("Hello, World!")

if __name__ == "__main__":
    hello()
'''
        result = await mcp_registry.write_file("hello.py", content)

        assert "Error:" not in result
        file_path = temp_project_root / "hello.py"
        assert file_path.read_text(encoding="utf-8") == content

    async def test_write_json_content(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing JSON content."""
        content = '{"name": "test", "value": 123, "nested": {"key": "value"}}'
        result = await mcp_registry.write_file("data.json", content)

        assert "Error:" not in result
        file_path = temp_project_root / "data.json"
        assert file_path.read_text(encoding="utf-8") == content


class TestWriteFilePathHandling:
    """Test various path formats and security."""

    async def test_write_with_relative_path(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing with relative path."""
        result = await mcp_registry.write_file("./relative/path.txt", "content")

        assert "Error:" not in result
        file_path = temp_project_root / "relative" / "path.txt"
        assert file_path.exists()

    async def test_write_prevents_directory_traversal(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test that directory traversal attacks are prevented."""
        result = await mcp_registry.write_file("../../../etc/malicious.txt", "bad")

        assert "Error:" in result or "denied" in result.lower() or "Security" in result

        malicious_path = (
            temp_project_root.parent.parent.parent / "etc" / "malicious.txt"
        )
        assert not malicious_path.exists()

    async def test_write_with_special_characters_in_filename(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing files with special characters in names."""
        result = await mcp_registry.write_file("file-with_special@chars.txt", "content")

        assert "Error:" not in result
        file_path = temp_project_root / "file-with_special@chars.txt"
        assert file_path.exists()

    async def test_write_with_spaces_in_filename(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing files with spaces in names."""
        result = await mcp_registry.write_file("file with spaces.txt", "content")

        assert "Error:" not in result
        file_path = temp_project_root / "file with spaces.txt"
        assert file_path.exists()


class TestWriteFileErrorHandling:
    """Test error handling and edge cases."""

    @pytest.mark.skipif(
        os.name == "nt", reason="chmod 0o444 does not prevent file creation on Windows"
    )
    async def test_write_to_readonly_directory(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing to a read-only directory."""
        readonly_dir = temp_project_root / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)

        try:
            result = await mcp_registry.write_file("readonly/file.txt", "content")

            assert "Error:" in result
        finally:
            readonly_dir.chmod(0o755)

    async def test_write_very_long_content(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing very long content."""
        content = "x" * (1024 * 1024)
        result = await mcp_registry.write_file("large.txt", content)

        assert "Error:" not in result
        file_path = temp_project_root / "large.txt"
        assert file_path.exists()
        assert len(file_path.read_text(encoding="utf-8")) == len(content)

    async def test_write_with_various_file_extensions(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test writing files with various extensions."""
        extensions = [".py", ".js", ".ts", ".md", ".json", ".yaml", ".toml", ".txt"]

        for ext in extensions:
            filename = f"test{ext}"
            result = await mcp_registry.write_file(filename, f"content for {ext}")

            assert "Error:" not in result
            file_path = temp_project_root / filename
            assert file_path.exists()
