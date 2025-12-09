from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
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
    mock_ingestor = MagicMock()
    mock_cypher_gen = MagicMock()

    registry = MCPToolsRegistry(
        project_root=str(temp_project_root),
        ingestor=mock_ingestor,
        cypher_gen=mock_cypher_gen,
    )

    return registry


class TestSurgicalReplaceBasic:
    async def test_replace_function_implementation(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        target = '    print("Hello, World!")'
        replacement = '    print("Hello, Universe!")'

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Success" in result
        content = (temp_project_root / "sample.py").read_text()
        assert 'print("Hello, Universe!")' in content

    async def test_replace_method_implementation(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
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

        assert "Success" in result

    async def test_replace_with_exact_match(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        target = '    print("Hello, World!")'
        replacement = '    print("Goodbye!")'

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Success" in result
        content = (temp_project_root / "sample.py").read_text()
        assert 'print("Goodbye!")' in content


class TestSurgicalReplaceEdgeCases:
    async def test_replace_with_unicode(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        unicode_file = temp_project_root / "unicode.py"
        unicode_file.write_text(
            'def greet():\n    print("Hello 世界")\n', encoding="utf-8"
        )

        target = 'print("Hello 世界")'
        replacement = 'print("你好 世界")'

        result = await mcp_registry.surgical_replace_code(
            "unicode.py", target, replacement
        )

        assert "Success" in result

    async def test_replace_preserves_whitespace(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        target = "    def add(self, a: int, b: int) -> int:"
        replacement = "    def multiply(self, a: int, b: int) -> int:"

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Success" in result
        content = (temp_project_root / "sample.py").read_text()
        assert "def multiply(self, a: int, b: int) -> int:" in content


class TestSurgicalReplaceErrorHandling:
    async def test_replace_nonexistent_file(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        result = await mcp_registry.surgical_replace_code(
            "nonexistent.py", "target", "replacement"
        )

        assert "Failed" in result or "Error" in result

    async def test_replace_code_not_found(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        target = "def nonexistent_function():"
        replacement = "def new_function():"

        result = await mcp_registry.surgical_replace_code(
            "sample.py", target, replacement
        )

        assert "Failed" in result or "not found" in result.lower()

    async def test_replace_with_exception(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        mcp_registry.file_editor.replace_code_block = MagicMock(  # type: ignore[method-assign]
            side_effect=Exception("Permission denied")
        )

        result = await mcp_registry.surgical_replace_code(
            "sample.py", "target", "replacement"
        )

        assert "Error:" in result

    async def test_replace_readonly_file(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        readonly_file = temp_project_root / "readonly.py"
        readonly_file.write_text("def func(): pass", encoding="utf-8")
        readonly_file.chmod(0o444)

        try:
            result = await mcp_registry.surgical_replace_code(
                "readonly.py", "def func():", "def new_func():"
            )

            assert "Error" in result or "Failed" in result
        finally:
            readonly_file.chmod(0o644)


class TestSurgicalReplacePathHandling:
    async def test_replace_in_subdirectory(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        subdir = temp_project_root / "subdir"
        subdir.mkdir()
        sub_file = subdir / "module.py"
        sub_file.write_text("def func(): pass", encoding="utf-8")

        result = await mcp_registry.surgical_replace_code(
            "subdir/module.py", "def func():", "def new_func():"
        )

        assert "Success" in result

    async def test_replace_prevents_directory_traversal(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        result = await mcp_registry.surgical_replace_code(
            "../../../etc/passwd", "root:", "hacked:"
        )

        assert "Error" in result or "Failed" in result or "Security" in result


class TestSurgicalReplaceIntegration:
    async def test_multiple_replacements_in_sequence(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        result1 = await mcp_registry.surgical_replace_code(
            "sample.py", 'print("Hello, World!")', 'print("Hi!")'
        )
        assert "Success" in result1

    async def test_replace_verifies_parameters_passed(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        original_replace = mcp_registry.file_editor.replace_code_block
        call_args: dict[str, str] = {}

        def capture_args(*args: str, **kwargs: str) -> bool:
            call_args["file_path"] = args[0]
            call_args["target_code"] = args[1]
            call_args["replacement_code"] = args[2]
            return original_replace(*args, **kwargs)  # type: ignore[return-value]

        mcp_registry.file_editor.replace_code_block = capture_args  # type: ignore[method-assign]

        test_file = temp_project_root / "test.py"
        test_file.write_text("old_code = 1", encoding="utf-8")

        await mcp_registry.surgical_replace_code("test.py", "old_code", "new_code")

        assert call_args["file_path"] == "test.py"
        assert call_args["target_code"] == "old_code"
        assert call_args["replacement_code"] == "new_code"

    async def test_replace_different_file_types(
        self, mcp_registry: MCPToolsRegistry, temp_project_root: Path
    ) -> None:
        files = {
            "script.js": "function hello() { console.log('hi'); }",
            "style.css": "body { color: blue; }",
            "config.json": '{"key": "value"}',
        }

        for filename, content in files.items():
            (temp_project_root / filename).write_text(content, encoding="utf-8")

            result = await mcp_registry.surgical_replace_code(
                filename, list(content.split())[0], "replacement"
            )

            assert result is not None
