from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import Tool

from codebase_rag.tools.file_editor import (
    EditResult,
    FileEditor,
    create_file_editor_tool,
)

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def file_editor(temp_project_root: Path) -> FileEditor:
    return FileEditor(str(temp_project_root))


@pytest.fixture
def sample_python_file(temp_project_root: Path) -> Path:
    file_path = temp_project_root / "sample.py"
    content = """def hello():
    return "Hello, World!"

def goodbye():
    return "Goodbye!"

class Greeter:
    def greet(self):
        return "Hi!"
"""
    file_path.write_text(content, encoding="utf-8")
    return file_path


@pytest.fixture
def sample_js_file(temp_project_root: Path) -> Path:
    file_path = temp_project_root / "sample.js"
    content = """function hello() {
    return "Hello, World!";
}

function goodbye() {
    return "Goodbye!";
}
"""
    file_path.write_text(content, encoding="utf-8")
    return file_path


class TestFileEditorInit:
    def test_init_resolves_project_root(self, temp_project_root: Path) -> None:
        editor = FileEditor(str(temp_project_root))
        assert editor.project_root == temp_project_root.resolve()

    def test_init_creates_dmp_instance(self, file_editor: FileEditor) -> None:
        assert file_editor.dmp is not None

    def test_init_loads_parsers(self, file_editor: FileEditor) -> None:
        assert file_editor.parsers is not None
        assert isinstance(file_editor.parsers, dict)


class TestEditResult:
    def test_success_result(self) -> None:
        result = EditResult(file_path="test.py", success=True)
        assert result.file_path == "test.py"
        assert result.success is True
        assert result.error_message is None

    def test_error_result(self) -> None:
        result = EditResult(
            file_path="test.py", success=False, error_message="Edit failed"
        )
        assert result.file_path == "test.py"
        assert result.success is False
        assert result.error_message == "Edit failed"


class TestGetParser:
    def test_get_parser_for_python(
        self, file_editor: FileEditor, sample_python_file: Path
    ) -> None:
        parser = file_editor.get_parser("sample.py")
        assert parser is not None

    def test_get_parser_for_javascript(
        self, file_editor: FileEditor, sample_js_file: Path
    ) -> None:
        parser = file_editor.get_parser("sample.js")
        assert parser is not None

    def test_get_parser_for_unknown_extension(self, file_editor: FileEditor) -> None:
        parser = file_editor.get_parser("file.unknown")
        assert parser is None


class TestGetAst:
    def test_get_ast_for_python_file(
        self, file_editor: FileEditor, sample_python_file: Path
    ) -> None:
        root_node = file_editor.get_ast(str(sample_python_file))
        assert root_node is not None
        assert root_node.type == "module"

    def test_get_ast_for_javascript_file(
        self, file_editor: FileEditor, sample_js_file: Path
    ) -> None:
        root_node = file_editor.get_ast(str(sample_js_file))
        assert root_node is not None
        assert root_node.type == "program"


class TestGetFunctionSourceCode:
    def test_get_function_source_by_name(
        self, file_editor: FileEditor, sample_python_file: Path
    ) -> None:
        source = file_editor.get_function_source_code(str(sample_python_file), "hello")
        assert source is not None
        assert "def hello():" in source
        assert 'return "Hello, World!"' in source

    def test_get_function_source_by_qualified_name(
        self, file_editor: FileEditor, sample_python_file: Path
    ) -> None:
        source = file_editor.get_function_source_code(
            str(sample_python_file), "Greeter.greet"
        )
        assert source is not None
        assert "def greet(self):" in source

    def test_get_nonexistent_function(
        self, file_editor: FileEditor, sample_python_file: Path
    ) -> None:
        source = file_editor.get_function_source_code(
            str(sample_python_file), "nonexistent"
        )
        assert source is None


class TestReplaceCodeBlock:
    def test_replace_existing_block(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "replace_test.py"
        test_file.write_text("old_value = 1\nkeep_this = 2", encoding="utf-8")
        success = file_editor.replace_code_block(
            "replace_test.py", "old_value = 1", "new_value = 100"
        )
        assert success is True
        content = test_file.read_text(encoding="utf-8")
        assert "new_value = 100" in content
        assert "keep_this = 2" in content

    def test_replace_nonexistent_block(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "replace_test.py"
        test_file.write_text("existing_code = True", encoding="utf-8")
        success = file_editor.replace_code_block(
            "replace_test.py", "nonexistent_code", "replacement"
        )
        assert success is False

    def test_replace_block_file_not_found(self, file_editor: FileEditor) -> None:
        success = file_editor.replace_code_block(
            "nonexistent.py", "target", "replacement"
        )
        assert success is False

    def test_replace_block_outside_root(self, file_editor: FileEditor) -> None:
        success = file_editor.replace_code_block(
            "../../../etc/passwd", "target", "replacement"
        )
        assert success is False

    def test_replace_identical_content(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "identical_test.py"
        test_file.write_text("same_content = True", encoding="utf-8")
        success = file_editor.replace_code_block(
            "identical_test.py", "same_content = True", "same_content = True"
        )
        assert success is False


class TestEditFile:
    async def test_edit_existing_file(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "edit_test.py"
        test_file.write_text("original", encoding="utf-8")
        result = await file_editor.edit_file("edit_test.py", "new content")
        assert result.success is True
        assert test_file.read_text(encoding="utf-8") == "new content"

    async def test_edit_nonexistent_file(self, file_editor: FileEditor) -> None:
        result = await file_editor.edit_file("nonexistent.py", "content")
        assert result.success is False
        assert result.error_message is not None

    async def test_edit_file_outside_root(self, file_editor: FileEditor) -> None:
        result = await file_editor.edit_file("../../../tmp/malicious.py", "content")
        assert result.success is False
        assert "security" in str(result.error_message).lower()

    async def test_edit_directory_fails(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        subdir = temp_project_root / "subdir"
        subdir.mkdir()
        result = await file_editor.edit_file("subdir", "content")
        assert result.success is False


class TestGetDiff:
    def test_get_diff_shows_changes(
        self, file_editor: FileEditor, sample_python_file: Path
    ) -> None:
        new_code = """def hello():
    return "Changed!"
"""
        diff = file_editor.get_diff(str(sample_python_file), "hello", new_code)
        assert diff is not None
        assert "Hello, World!" in diff or "Changed!" in diff
        assert "-" in diff or "+" in diff

    def test_get_diff_nonexistent_function(
        self, file_editor: FileEditor, sample_python_file: Path
    ) -> None:
        diff = file_editor.get_diff(str(sample_python_file), "nonexistent", "new code")
        assert diff is None


class TestApplyPatchToFile:
    def test_apply_valid_patch(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "patch_test.txt"
        original = "Hello World"
        test_file.write_text(original, encoding="utf-8")

        patches = file_editor.dmp.patch_make(original, "Hello Universe")
        patch_text = file_editor.dmp.patch_toText(patches)

        success = file_editor.apply_patch_to_file(str(test_file), patch_text)
        assert success is True
        assert test_file.read_text(encoding="utf-8") == "Hello Universe"


class TestCreateFileEditorTool:
    def test_creates_tool_instance(self, file_editor: FileEditor) -> None:
        tool = create_file_editor_tool(file_editor)
        assert isinstance(tool, Tool)

    def test_tool_has_description(self, file_editor: FileEditor) -> None:
        tool = create_file_editor_tool(file_editor)
        assert tool.description is not None
        assert (
            "replace" in tool.description.lower()
            or "surgical" in tool.description.lower()
        )

    def test_tool_requires_approval(self, file_editor: FileEditor) -> None:
        tool = create_file_editor_tool(file_editor)
        assert tool.requires_approval is True

    async def test_tool_function_replaces_code(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "tool_test.py"
        test_file.write_text("old_code = 1", encoding="utf-8")
        tool = create_file_editor_tool(file_editor)
        result = await tool.function(
            file_path="tool_test.py",
            target_code="old_code = 1",
            replacement_code="new_code = 2",
        )
        assert "success" in result.lower()
        assert test_file.read_text(encoding="utf-8") == "new_code = 2"

    async def test_tool_function_returns_failure_message(
        self, file_editor: FileEditor
    ) -> None:
        tool = create_file_editor_tool(file_editor)
        result = await tool.function(
            file_path="nonexistent.py",
            target_code="target",
            replacement_code="replacement",
        )
        assert "failed" in result.lower()
