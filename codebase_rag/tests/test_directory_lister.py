from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import Tool

from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)


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


class TestGetSafePath:
    def test_safe_path_with_relative_path(
        self, directory_lister: DirectoryLister, temp_project_root: Path
    ) -> None:
        safe_path = directory_lister._get_safe_path("subdir")
        assert safe_path == (temp_project_root / "subdir").resolve()

    def test_safe_path_with_absolute_path_within_root(
        self, directory_lister: DirectoryLister, temp_project_root: Path
    ) -> None:
        abs_path = str(temp_project_root / "subdir")
        safe_path = directory_lister._get_safe_path(abs_path)
        assert safe_path == (temp_project_root / "subdir").resolve()

    def test_safe_path_rejects_path_outside_root(
        self, directory_lister: DirectoryLister
    ) -> None:
        with pytest.raises(PermissionError):
            directory_lister._get_safe_path("../../../etc")

    def test_safe_path_rejects_absolute_path_outside_root(
        self, directory_lister: DirectoryLister
    ) -> None:
        with pytest.raises(PermissionError):
            directory_lister._get_safe_path("/etc/passwd")


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
