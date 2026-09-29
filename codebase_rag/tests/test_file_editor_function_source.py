from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tools.file_editor import FileEditor

SOURCE = """class Alpha:
    def run(self):
        return "alpha"


class Beta:
    def run(self):
        return "beta"


def solo():
    return 1
"""


@pytest.fixture
def module_path(tmp_path: Path) -> str:
    path = tmp_path / "mod.py"
    path.write_text(SOURCE, encoding="utf-8")
    return str(path)


@pytest.fixture
def editor(tmp_path: Path) -> FileEditor:
    return FileEditor(str(tmp_path))


def test_a_unique_name_returns_its_source(editor: FileEditor, module_path: str) -> None:
    source = editor.get_function_source_code(module_path, "solo")
    assert source is not None
    assert source.startswith("def solo")


def test_an_unknown_name_returns_none(editor: FileEditor, module_path: str) -> None:
    assert editor.get_function_source_code(module_path, "missing") is None


def test_a_line_number_picks_between_same_named_methods(
    editor: FileEditor, module_path: str
) -> None:
    source = editor.get_function_source_code(module_path, "run", line_number=7)
    assert source is not None
    assert "beta" in source


def test_a_line_matching_no_candidate_returns_none(
    editor: FileEditor, module_path: str
) -> None:
    assert editor.get_function_source_code(module_path, "run", line_number=99) is None


def test_a_qualified_name_picks_its_class(editor: FileEditor, module_path: str) -> None:
    source = editor.get_function_source_code(module_path, "Beta.run")
    assert source is not None
    assert "beta" in source


def test_an_unmatched_qualified_name_returns_none(
    editor: FileEditor, module_path: str
) -> None:
    assert editor.get_function_source_code(module_path, "Gamma.run") is None


def test_an_ambiguous_bare_name_falls_back_to_the_first(
    editor: FileEditor, module_path: str
) -> None:
    source = editor.get_function_source_code(module_path, "run")
    assert source is not None
    assert "alpha" in source
