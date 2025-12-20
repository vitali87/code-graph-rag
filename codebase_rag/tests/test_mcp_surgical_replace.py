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
    """Create a temporary project root directory with sample files."""
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

    def subtract(self, a: int, b: int) -> int:
        """Subtract two numbers."""
        return a - b
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

    registry._file_editor_tool = MagicMock()
    registry._file_editor_tool.function = AsyncMock()

    return registry


class TestSurgicalReplaceBasic:
    """Test basic code replacement functionality."""

    async def test_replace_function_implementation(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        mock_func = mcp_registry._file_editor_tool.function
        assert isinstance(mock_func, AsyncMock)
        mock_func.return_value = "Successfully replaced code in sample.py"

        target = '    print("Hello, World!")'
        replacement = '    print("Hello, Universe!")'

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Error:" not in result
        assert "Success" in result or "replaced" in result.lower()
        mock_func.assert_called_once()

    async def test_replace_method_implementation(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        mock_func = mcp_registry._file_editor_tool.function
        assert isinstance(mock_func, AsyncMock)
        mock_func.return_value = "Successfully replaced code in sample.py"

        target = """    def add(self, a: int, b: int) -> int:
        \"\"\"Add two numbers.\"\"\"
        return a + b"""

        replacement = """    def add(self, a: int, b: int) -> int:
        \"\"\"Add two numbers with logging.\"\"\"
        print(f"Adding {a} + {b}")
        return a + b"""

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Error:" not in result
        mock_func.assert_called_once()

    async def test_replace_with_exact_match(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        mock_func = mcp_registry._file_editor_tool.function
        assert isinstance(mock_func, AsyncMock)
        mock_func.return_value = "Successfully replaced code in sample.py"

        target = 'print("Hello, World!")'
        replacement = 'print("Goodbye!")'

        await mcp_registry.surgical_replace_code("sample.py", target, replacement)

        call_args = mock_func.call_args
        assert call_args is not None
        assert call_args.kwargs["file_path"] == "sample.py"
        assert call_args.kwargs["target_code"] == target
        assert call_args.kwargs["replacement_code"] == replacement


class TestSurgicalReplaceEdgeCases:
    """Test edge cases and special scenarios."""

    async def test_replace_with_unicode(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test replacing code with unicode characters."""
        unicode_file = temp_project_root / "unicode.py"
        unicode_file.write_text(
            'def greet():\n    print("Hello 世界")\n', encoding="utf-8"
        )

        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Successfully replaced code"
        )

        target = 'print("Hello 世界")'
        replacement = 'print("你好 世界")'

        result = await mcp_registry.surgical_replace_code(
            "unicode.py", target, replacement
        )

        assert "Error:" not in result

    async def test_replace_multiline_block(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test replacing a multiline code block."""
        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Successfully replaced code"
        )

        target = '''class Calculator:
    """Simple calculator class."""

    def add(self, a: int, b: int) -> int:
        """Add two numbers."""
        return a + b'''

        replacement = '''class Calculator:
    """Advanced calculator class."""

    def add(self, a: int, b: int) -> int:
        """Add two numbers with validation."""
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise TypeError("Arguments must be numbers")
        return a + b'''

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Error:" not in result

    async def test_replace_with_empty_replacement(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test replacing code with empty string (deletion)."""
        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Successfully replaced code"
        )

        target = '    print("Hello, World!")'
        replacement = ""

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Error:" not in result

    async def test_replace_preserves_whitespace(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        mock_func = mcp_registry._file_editor_tool.function
        assert isinstance(mock_func, AsyncMock)
        mock_func.return_value = "Successfully replaced code"

        target = "    def add(self, a: int, b: int) -> int:"
        replacement = "    def multiply(self, a: int, b: int) -> int:"

        await mcp_registry.surgical_replace_code("sample.py", target, replacement)

        call_args = mock_func.call_args
        assert call_args is not None
        assert call_args.kwargs["target_code"] == target
        assert call_args.kwargs["replacement_code"] == replacement


class TestSurgicalReplaceErrorHandling:
    """Test error handling and failure scenarios."""

    async def test_replace_nonexistent_file(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test replacing code in a nonexistent file."""
        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Error: File not found: nonexistent.py"
        )

        result = await mcp_registry.surgical_replace_code(
            "nonexistent.py", "target", "replacement"
        )

        assert "Error:" in result

    async def test_replace_code_not_found(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test replacing code that doesn't exist in the file."""
        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Error: Target code not found in file"
        )

        target = "def nonexistent_function():"
        replacement = "def new_function():"

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Error:" in result

    async def test_replace_with_exception(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test handling of exceptions during replacement."""
        mcp_registry._file_editor_tool.function.side_effect = Exception(  # ty: ignore[invalid-assignment]
            "Permission denied"
        )

        result = await mcp_registry.surgical_replace_code(
            "sample.py", "target", "replacement"
        )

        assert "Error:" in result

    async def test_replace_readonly_file(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test replacing code in a read-only file."""
        readonly_file = temp_project_root / "readonly.py"
        readonly_file.write_text("def func(): pass", encoding="utf-8")
        readonly_file.chmod(0o444)

        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Error: Permission denied"
        )

        try:
            result = await mcp_registry.surgical_replace_code(
                "readonly.py", "def func():", "def new_func():"
            )

            assert "Error:" in result
        finally:
            readonly_file.chmod(0o644)


class TestSurgicalReplacePathHandling:
    """Test path handling and security."""

    async def test_replace_in_subdirectory(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test replacing code in a file in a subdirectory."""
        subdir = temp_project_root / "subdir"
        subdir.mkdir()
        sub_file = subdir / "module.py"
        sub_file.write_text("def func(): pass", encoding="utf-8")

        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Successfully replaced code"
        )

        result = await mcp_registry.surgical_replace_code(
            "subdir/module.py", "def func():", "def new_func():"
        )

        assert "Error:" not in result

    async def test_replace_prevents_directory_traversal(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test that directory traversal is prevented."""
        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Error: Security risk - path traversal detected"
        )

        result = await mcp_registry.surgical_replace_code(
            "../../../etc/passwd", "root:", "hacked:"
        )

        assert "Error:" in result or "Security" in result


class TestSurgicalReplaceIntegration:
    """Test integration scenarios."""

    async def test_multiple_replacements_in_sequence(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test performing multiple replacements sequentially."""
        mcp_registry._file_editor_tool.function.return_value = (  # ty: ignore[invalid-assignment]
            "Successfully replaced code"
        )

        replacements = [
            ('print("Hello, World!")', 'print("Hi!")'),
            ("def add(", "def addition("),
            ("def subtract(", "def subtraction("),
        ]

        for target, replacement in replacements:
            result = await mcp_registry.surgical_replace_code(
                "sample.py", target, replacement
            )
            assert "Error:" not in result

    async def test_replace_verifies_parameters_passed(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        mock_func = mcp_registry._file_editor_tool.function
        assert isinstance(mock_func, AsyncMock)
        mock_func.return_value = "Success"

        await mcp_registry.surgical_replace_code("test.py", "old_code", "new_code")

        mock_func.assert_called_once_with(
            file_path="test.py", target_code="old_code", replacement_code="new_code"
        )

    async def test_replace_different_file_types(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test replacing code in different file types."""
        files = {
            "script.js": "function hello() { console.log('hi'); }",
            "style.css": "body { color: blue; }",
            "config.json": '{"key": "value"}',
        }

        for filename, content in files.items():
            (temp_project_root / filename).write_text(content, encoding="utf-8")
            mcp_registry._file_editor_tool.function.return_value = "Success"  # ty: ignore[invalid-assignment]

            result = await mcp_registry.surgical_replace_code(
                filename, list(content.split())[0], "replacement"
            )

            assert result is not None
