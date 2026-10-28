from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai import Tool

from codebase_rag import tool_errors as te
from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)
from codebase_rag.utils.path_utils import resolve_in_root


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def directory_lister(temp_project_root: Path) -> DirectoryLister:
    return DirectoryLister(str(temp_project_root))


@pytest.fixture
def sample_directory_structure(temp_project_root: Path) -> Path:
    (temp_project_root / "file1.txt").write_text("content1", encoding="utf-8")
    (temp_project_root / "file2.py").write_text("content2", encoding="utf-8")
    (temp_project_root / "subdir1").mkdir()
    (temp_project_root / "subdir2").mkdir()
    nested = temp_project_root / "subdir1" / "nested"
    nested.mkdir(parents=True)
    (nested / "nested_file.txt").write_text("nested content", encoding="utf-8")
    return temp_project_root


class TestDirectoryListerInit:
    def test_init_resolves_project_root(self, temp_project_root: Path) -> None:
        lister = DirectoryLister(str(temp_project_root))
        assert lister.project_root == temp_project_root.resolve()

    def test_init_with_relative_path(self) -> None:
        lister = DirectoryLister(".")
        assert lister.project_root == Path(".").resolve()


class TestListDirectoryContents:
    def test_list_directory_contents_are_sorted(
        self, directory_lister: DirectoryLister
    ) -> None:
        with patch(
            "codebase_rag.tools.directory_lister.os.listdir",
            return_value=["zeta.py", "alpha.py", "middle.py"],
        ):
            result = directory_lister.list_directory_contents(".")

        assert result.splitlines() == ["alpha.py", "middle.py", "zeta.py"]

    def test_list_root_directory(
        self, directory_lister: DirectoryLister, sample_directory_structure: Path
    ) -> None:
        result = directory_lister.list_directory_contents(".")
        assert "file1.txt" in result
        assert "file2.py" in result
        assert "subdir1" in result
        assert "subdir2" in result

    def test_list_subdirectory(
        self, directory_lister: DirectoryLister, sample_directory_structure: Path
    ) -> None:
        result = directory_lister.list_directory_contents("subdir1")
        assert "nested" in result
        assert "file1.txt" not in result

    def test_list_nested_directory(
        self, directory_lister: DirectoryLister, sample_directory_structure: Path
    ) -> None:
        result = directory_lister.list_directory_contents("subdir1/nested")
        assert "nested_file.txt" in result

    def test_list_empty_directory(
        self, directory_lister: DirectoryLister, temp_project_root: Path
    ) -> None:
        empty_dir = temp_project_root / "empty"
        empty_dir.mkdir()
        result = directory_lister.list_directory_contents("empty")
        assert "empty" in result.lower()

    def test_list_nonexistent_directory(
        self, directory_lister: DirectoryLister
    ) -> None:
        result = directory_lister.list_directory_contents("nonexistent")
        assert "not a valid directory" in result

    def test_list_file_instead_of_directory(
        self, directory_lister: DirectoryLister, sample_directory_structure: Path
    ) -> None:
        result = directory_lister.list_directory_contents("file1.txt")
        assert "not a valid directory" in result

    def test_list_with_absolute_path_within_root(
        self, directory_lister: DirectoryLister, sample_directory_structure: Path
    ) -> None:
        abs_path = str(sample_directory_structure / "subdir1")
        result = directory_lister.list_directory_contents(abs_path)
        assert "nested" in result

    def test_list_with_special_characters(
        self, directory_lister: DirectoryLister, temp_project_root: Path
    ) -> None:
        special_dir = temp_project_root / "special-dir_123"
        special_dir.mkdir()
        (special_dir / "file with spaces.txt").write_text("content", encoding="utf-8")
        result = directory_lister.list_directory_contents("special-dir_123")
        assert "file with spaces.txt" in result

    def test_list_with_hidden_files(
        self, directory_lister: DirectoryLister, temp_project_root: Path
    ) -> None:
        hidden_dir = temp_project_root / "hidden"
        hidden_dir.mkdir()
        (hidden_dir / ".hidden_file").write_text("hidden", encoding="utf-8")
        (hidden_dir / "visible_file").write_text("visible", encoding="utf-8")
        result = directory_lister.list_directory_contents("hidden")
        assert ".hidden_file" in result
        assert "visible_file" in result

    def test_list_directory_returns_error_for_path_outside_root(
        self, directory_lister: DirectoryLister
    ) -> None:
        result = directory_lister.list_directory_contents("../../../etc")
        expected = te.failure(
            te.PATH_OUTSIDE_ROOT.format(
                path="../../../etc", root=directory_lister.project_root
            )
        )
        assert result == expected

    def test_list_directory_returns_error_for_absolute_path_outside_root(
        self, directory_lister: DirectoryLister
    ) -> None:
        result = directory_lister.list_directory_contents("/etc/passwd")
        expected = te.failure(
            te.PATH_OUTSIDE_ROOT.format(
                path="/etc/passwd", root=directory_lister.project_root
            )
        )
        assert result == expected


class TestResolveInRoot:
    def test_a_relative_path_resolves_under_the_root(
        self, temp_project_root: Path
    ) -> None:
        resolved = resolve_in_root(temp_project_root, "subdir")
        assert resolved == (temp_project_root / "subdir").resolve()

    def test_an_absolute_path_within_the_root_resolves(
        self, temp_project_root: Path
    ) -> None:
        abs_path = str(temp_project_root / "subdir")
        resolved = resolve_in_root(temp_project_root, abs_path)
        assert resolved == (temp_project_root / "subdir").resolve()

    def test_a_traversal_out_of_the_root_is_none(self, temp_project_root: Path) -> None:
        assert resolve_in_root(temp_project_root, "../../../etc") is None

    def test_an_absolute_path_outside_the_root_is_none(
        self, temp_project_root: Path
    ) -> None:
        assert resolve_in_root(temp_project_root, "/etc/passwd") is None

    def test_an_unresolvable_path_is_none_not_an_exception(
        self, temp_project_root: Path
    ) -> None:
        # The lister caught only ValueError, so resolve()'s RuntimeError on
        # a link loop escaped the tool.
        with patch.object(Path, "resolve", side_effect=RuntimeError("loop")):
            assert resolve_in_root(temp_project_root, "loop") is None

    def test_the_lister_refuses_an_unresolvable_path(
        self, directory_lister: DirectoryLister
    ) -> None:
        with patch.object(Path, "resolve", side_effect=RuntimeError("loop")):
            result = directory_lister.list_directory_contents("loop")
        assert isinstance(result, te.ToolFailure)


class TestCreateDirectoryListerTool:
    def test_creates_tool_instance(self, directory_lister: DirectoryLister) -> None:
        tool = create_directory_lister_tool(directory_lister)
        assert isinstance(tool, Tool)

    def test_tool_has_description(self, directory_lister: DirectoryLister) -> None:
        tool = create_directory_lister_tool(directory_lister)
        assert tool.description is not None
        assert "directory" in tool.description.lower()

    def test_tool_function_returns_contents(
        self, directory_lister: DirectoryLister, sample_directory_structure: Path
    ) -> None:
        tool = create_directory_lister_tool(directory_lister)
        result = tool.function(".")
        assert "file1.txt" in result
        assert "subdir1" in result
