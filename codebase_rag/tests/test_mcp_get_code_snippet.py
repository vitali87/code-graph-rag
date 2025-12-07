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
    # Create a sample Python file
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
def mcp_registry(temp_project_root: Path) -> MCPToolsRegistry:
    """Create an MCP tools registry with mocked dependencies."""
    mock_ingestor = MagicMock()
    mock_cypher_gen = MagicMock()

    registry = MCPToolsRegistry(
        project_root=str(temp_project_root),
        ingestor=mock_ingestor,
        cypher_gen=mock_cypher_gen,
    )

    # Mock the code retrieval tool
    registry._code_tool = MagicMock()
    registry._code_tool.function = AsyncMock()

    return registry


class TestGetCodeSnippetBasic:
    """Test basic code snippet retrieval functionality."""

    async def test_get_function_snippet(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving a function snippet."""
        # Mock the response from the code tool
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "sample.hello_world",
                "source_code": 'def hello_world():\n    """Say hello to the world."""\n    print("Hello, World!")\n',
                "file_path": "sample.py",
                "line_start": 1,
                "line_end": 3,
                "docstring": "Say hello to the world.",
                "found": True,
            }
        )

        result = await mcp_registry.get_code_snippet("sample.hello_world")

        assert result["found"] is True
        assert result["qualified_name"] == "sample.hello_world"
        assert "def hello_world()" in result["source_code"]
        assert result["file_path"] == "sample.py"
        assert result["line_start"] == 1
        assert result["line_end"] == 3
        assert result["docstring"] == "Say hello to the world."

    async def test_get_method_snippet(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving a class method snippet."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "sample.Calculator.add",
                "source_code": '    def add(self, a: int, b: int) -> int:\n        """Add two numbers."""\n        return a + b\n',
                "file_path": "sample.py",
                "line_start": 8,
                "line_end": 10,
                "docstring": "Add two numbers.",
                "found": True,
            }
        )

        result = await mcp_registry.get_code_snippet("sample.Calculator.add")

        assert result["found"] is True
        assert result["qualified_name"] == "sample.Calculator.add"
        assert "def add" in result["source_code"]
        assert result["docstring"] == "Add two numbers."

    async def test_get_class_snippet(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving a class snippet."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "sample.Calculator",
                "source_code": 'class Calculator:\n    """Simple calculator class."""\n\n    def add(self, a: int, b: int) -> int:\n        """Add two numbers."""\n        return a + b\n',
                "file_path": "sample.py",
                "line_start": 5,
                "line_end": 10,
                "docstring": "Simple calculator class.",
                "found": True,
            }
        )

        result = await mcp_registry.get_code_snippet("sample.Calculator")

        assert result["found"] is True
        assert result["qualified_name"] == "sample.Calculator"
        assert "class Calculator" in result["source_code"]
        assert result["docstring"] == "Simple calculator class."


class TestGetCodeSnippetNotFound:
    """Test handling of code snippets that don't exist."""

    async def test_get_nonexistent_function(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test retrieving a nonexistent function."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "nonexistent.function",
                "source_code": "",
                "file_path": "",
                "line_start": 0,
                "line_end": 0,
                "found": False,
                "error_message": "Entity not found in graph.",
            }
        )

        result = await mcp_registry.get_code_snippet("nonexistent.function")

        assert result["found"] is False
        assert result["error_message"] == "Entity not found in graph."
        assert result["source_code"] == ""

    async def test_get_malformed_qualified_name(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test retrieving with malformed qualified name."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "..invalid..",
                "source_code": "",
                "file_path": "",
                "line_start": 0,
                "line_end": 0,
                "found": False,
                "error_message": "Invalid qualified name format.",
            }
        )

        result = await mcp_registry.get_code_snippet("..invalid..")

        assert result["found"] is False
        assert "error_message" in result


class TestGetCodeSnippetEdgeCases:
    """Test edge cases and special scenarios."""

    async def test_get_snippet_with_no_docstring(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test retrieving code with no docstring."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "sample.no_docstring",
                "source_code": "def no_docstring():\n    pass\n",
                "file_path": "sample.py",
                "line_start": 12,
                "line_end": 13,
                "docstring": None,
                "found": True,
            }
        )

        result = await mcp_registry.get_code_snippet("sample.no_docstring")

        assert result["found"] is True
        assert result["docstring"] is None

    async def test_get_snippet_from_nested_module(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test retrieving code from deeply nested module."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "pkg.subpkg.module.ClassName.method",
                "source_code": "    def method(self):\n        return True\n",
                "file_path": "pkg/subpkg/module.py",
                "line_start": 10,
                "line_end": 11,
                "docstring": None,
                "found": True,
            }
        )

        result = await mcp_registry.get_code_snippet(
            "pkg.subpkg.module.ClassName.method"
        )

        assert result["found"] is True
        assert result["qualified_name"] == "pkg.subpkg.module.ClassName.method"
        assert result["file_path"] == "pkg/subpkg/module.py"

    async def test_get_snippet_with_unicode(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test retrieving code with unicode characters."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "sample.unicode_func",
                "source_code": 'def unicode_func():\n    """返回 Unicode 字符串。"""\n    return "Hello 世界"\n',
                "file_path": "sample.py",
                "line_start": 15,
                "line_end": 17,
                "docstring": "返回 Unicode 字符串。",
                "found": True,
            }
        )

        result = await mcp_registry.get_code_snippet("sample.unicode_func")

        assert result["found"] is True
        assert "世界" in result["source_code"]
        assert "Unicode" in result["docstring"]


class TestGetCodeSnippetErrorHandling:
    """Test error handling."""

    async def test_get_snippet_with_exception(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test handling of exceptions during retrieval."""
        mcp_registry._code_tool.function.side_effect = Exception("Database error")  # ty: ignore[invalid-assignment]

        result = await mcp_registry.get_code_snippet("sample.function")

        assert result["found"] is False
        assert "error" in result or "error_message" in result

    async def test_get_snippet_tool_returns_none(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test handling when tool returns None."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: None
        )

        # Should handle gracefully without crashing
        result = await mcp_registry.get_code_snippet("sample.function")

        # Should return an error dict
        assert isinstance(result, dict)


class TestGetCodeSnippetIntegration:
    """Test integration scenarios."""

    async def test_get_multiple_snippets_sequentially(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test retrieving multiple snippets in sequence."""
        snippets = [
            {
                "qualified_name": "module.func1",
                "source_code": "def func1(): pass",
                "file_path": "module.py",
                "line_start": 1,
                "line_end": 1,
                "found": True,
            },
            {
                "qualified_name": "module.func2",
                "source_code": "def func2(): pass",
                "file_path": "module.py",
                "line_start": 3,
                "line_end": 3,
                "found": True,
            },
        ]

        for snippet_data in snippets:
            mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
                model_dump=lambda s=snippet_data: s
            )
            qualified_name: str = snippet_data["qualified_name"]  # type: ignore[assignment]
            result = await mcp_registry.get_code_snippet(qualified_name)
            assert result["found"] is True
            assert result["qualified_name"] == snippet_data["qualified_name"]

    async def test_get_snippet_verifies_qualified_name_passed(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test that qualified name is correctly passed to underlying tool."""
        mcp_registry._code_tool.function.return_value = MagicMock(  # ty: ignore[invalid-assignment]
            model_dump=lambda: {
                "qualified_name": "test.function",
                "source_code": "def function(): pass",
                "file_path": "test.py",
                "line_start": 1,
                "line_end": 1,
                "found": True,
            }
        )

        await mcp_registry.get_code_snippet("test.function")

        # Verify the tool was called with the correct qualified name
        mcp_registry._code_tool.function.assert_called_once_with(
            qualified_name="test.function"
        )
