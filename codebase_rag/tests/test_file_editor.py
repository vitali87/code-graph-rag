from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import Tool

from codebase_rag import constants as cs
from codebase_rag import tool_errors as te
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tools.file_editor import (
    EditResult,
    FileEditor,
    create_file_editor_tool,
)


def _skip_unless_javascript_grammar() -> None:
    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.JS not in parsers:
        pytest.skip("javascript parser not available")


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


class TestReplaceCodeBlock:
    def test_replace_existing_block(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "replace_test.py"
        test_file.write_text("old_value = 1\nkeep_this = 2", encoding="utf-8")
        success = file_editor.apply_code_block(
            "replace_test.py", "old_value = 1", "new_value = 100"
        ).success
        assert success is True
        content = test_file.read_text(encoding="utf-8")
        assert "new_value = 100" in content
        assert "keep_this = 2" in content

    def test_replace_nonexistent_block(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "replace_test.py"
        test_file.write_text("existing_code = True", encoding="utf-8")
        success = file_editor.apply_code_block(
            "replace_test.py", "nonexistent_code", "replacement"
        ).success
        assert success is False

    def test_replace_block_file_not_found(self, file_editor: FileEditor) -> None:
        success = file_editor.apply_code_block(
            "nonexistent.py", "target", "replacement"
        ).success
        assert success is False

    def test_replace_block_outside_root(self, file_editor: FileEditor) -> None:
        # It used to answer the generic "target not found" here.
        result = file_editor.apply_code_block(
            "../../../etc/passwd", "target", "replacement"
        )
        assert result.success is False
        assert result.error_message == te.PATH_OUTSIDE_ROOT.format(
            path="../../../etc/passwd", root=file_editor.project_root
        )

    def test_an_undecodable_file_is_not_reported_as_outside_the_root(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        # UnicodeDecodeError is a ValueError, and the `except ValueError`
        # meant for the containment check wrapped the whole edit.
        (temp_project_root / "latin.py").write_bytes(b"name = '\xe9'\n")
        result = file_editor.apply_code_block("latin.py", "name", "other")
        assert result.success is False
        assert result.error_message == cs.MSG_SURGICAL_FAILED.format(path="latin.py")

    def test_replace_identical_content(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "identical_test.py"
        test_file.write_text("same_content = True", encoding="utf-8")
        success = file_editor.apply_code_block(
            "identical_test.py", "same_content = True", "same_content = True"
        ).success
        assert success is False


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

    async def test_concurrent_tool_calls_keep_every_edit(
        self, file_editor: FileEditor, temp_project_root: Path
    ) -> None:
        test_file = temp_project_root / "concurrent.py"
        test_file.write_text("a = 1\nb = 2\n", encoding="utf-8")
        tool = create_file_editor_tool(file_editor)
        results = await asyncio.gather(
            tool.function(
                file_path="concurrent.py",
                target_code="a = 1",
                replacement_code="a = 10",
            ),
            tool.function(
                file_path="concurrent.py",
                target_code="b = 2",
                replacement_code="b = 20",
            ),
        )
        assert all("success" in r.lower() for r in results)
        assert test_file.read_text(encoding="utf-8") == "a = 10\nb = 20\n"
