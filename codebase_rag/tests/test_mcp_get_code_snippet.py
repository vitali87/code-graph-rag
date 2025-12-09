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

    return registry


class TestGetCodeSnippetBasic:
    """Test basic code snippet retrieval functionality."""

    async def test_get_function_snippet(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving a function snippet."""
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
        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {
                "name": "add",
                "start": 8,
                "end": 10,
                "path": "sample.py",
                "docstring": "Add two numbers.",
            }
        ]

        result = await mcp_registry.get_code_snippet("sample.Calculator.add")

        assert result["found"] is True
        assert result["qualified_name"] == "sample.Calculator.add"
        assert "def add" in result["source_code"]
        assert result["docstring"] == "Add two numbers."

    async def test_get_class_snippet(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving a class snippet."""
        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {
                "name": "Calculator",
                "start": 5,
                "end": 10,
                "path": "sample.py",
                "docstring": "Simple calculator class.",
            }
        ]

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
        mcp_registry.ingestor.fetch_all.return_value = []  # type: ignore[attr-defined]

        result = await mcp_registry.get_code_snippet("nonexistent.function")

        assert result["found"] is False
        assert result["error_message"] == "Entity not found in graph."
        assert result["source_code"] == ""

    async def test_get_malformed_qualified_name(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test retrieving with malformed qualified name."""
        mcp_registry.ingestor.fetch_all.return_value = []  # type: ignore[attr-defined]

        result = await mcp_registry.get_code_snippet("..invalid..")

        assert result["found"] is False
        assert "error_message" in result


class TestGetCodeSnippetEdgeCases:
    """Test edge cases and special scenarios."""

    async def test_get_snippet_with_no_docstring(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving code with no docstring."""
        # (H) Add a file with no docstring function
        nodoc_file = temp_project_root / "nodoc.py"
        nodoc_file.write_text("def no_docstring():\n    pass\n", encoding="utf-8")

        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {
                "name": "no_docstring",
                "start": 1,
                "end": 2,
                "path": "nodoc.py",
                "docstring": None,
            }
        ]

        result = await mcp_registry.get_code_snippet("sample.no_docstring")

        assert result["found"] is True
        assert result["docstring"] is None

    async def test_get_snippet_from_nested_module(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving code from deeply nested module."""
        # (H) Create nested directory and file
        nested_dir = temp_project_root / "pkg" / "subpkg"
        nested_dir.mkdir(parents=True)
        nested_file = nested_dir / "module.py"
        nested_file.write_text(
            "class ClassName:\n    def method(self):\n        return True\n",
            encoding="utf-8",
        )

        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {
                "name": "method",
                "start": 2,
                "end": 3,
                "path": "pkg/subpkg/module.py",
                "docstring": None,
            }
        ]

        result = await mcp_registry.get_code_snippet(
            "pkg.subpkg.module.ClassName.method"
        )

        assert result["found"] is True
        assert result["qualified_name"] == "pkg.subpkg.module.ClassName.method"
        assert result["file_path"] == "pkg/subpkg/module.py"

    async def test_get_snippet_with_unicode(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving code with unicode characters."""
        # (H) Create file with unicode content
        unicode_file = temp_project_root / "unicode.py"
        unicode_file.write_text(
            'def unicode_func():\n    """返回 Unicode 字符串。"""\n    return "Hello 世界"\n',
            encoding="utf-8",
        )

        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {
                "name": "unicode_func",
                "start": 1,
                "end": 3,
                "path": "unicode.py",
                "docstring": "返回 Unicode 字符串。",
            }
        ]

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
        mcp_registry.ingestor.fetch_all.side_effect = Exception("Database error")  # type: ignore[attr-defined]

        result = await mcp_registry.get_code_snippet("sample.function")

        assert result["found"] is False
        assert "error" in result or "error_message" in result

    async def test_get_snippet_returns_empty_results(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Test handling when ingestor returns empty results."""
        mcp_registry.ingestor.fetch_all.return_value = []  # type: ignore[attr-defined]

        result = await mcp_registry.get_code_snippet("sample.function")

        assert isinstance(result, dict)
        assert result["found"] is False


class TestGetCodeSnippetIntegration:
    """Test integration scenarios."""

    async def test_get_multiple_snippets_sequentially(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test retrieving multiple snippets in sequence."""
        # (H) Create a module file with multiple functions
        module_file = temp_project_root / "module.py"
        module_file.write_text(
            "def func1(): pass\n\ndef func2(): pass\n", encoding="utf-8"
        )

        snippets = [
            {
                "qualified_name": "module.func1",
                "name": "func1",
                "start": 1,
                "end": 1,
                "path": "module.py",
            },
            {
                "qualified_name": "module.func2",
                "name": "func2",
                "start": 3,
                "end": 3,
                "path": "module.py",
            },
        ]

        for snippet_data in snippets:
            mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
                {
                    "name": snippet_data["name"],
                    "start": snippet_data["start"],
                    "end": snippet_data["end"],
                    "path": snippet_data["path"],
                }
            ]
            qualified_name = str(snippet_data["qualified_name"])
            result = await mcp_registry.get_code_snippet(qualified_name)
            assert result["found"] is True
            assert result["qualified_name"] == snippet_data["qualified_name"]

    async def test_get_snippet_verifies_qualified_name_passed(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        """Test that qualified name is correctly passed to underlying retriever."""
        test_file = temp_project_root / "test.py"
        test_file.write_text("def function(): pass\n", encoding="utf-8")

        mcp_registry.ingestor.fetch_all.return_value = [  # type: ignore[attr-defined]
            {
                "name": "function",
                "start": 1,
                "end": 1,
                "path": "test.py",
            }
        ]

        await mcp_registry.get_code_snippet("test.function")

        mcp_registry.ingestor.fetch_all.assert_called_once()  # type: ignore[attr-defined]
        call_args = mcp_registry.ingestor.fetch_all.call_args  # type: ignore[attr-defined]
        assert "test.function" in str(call_args)
