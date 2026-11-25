from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.constants import SupportedLanguage
from codebase_rag.models import LanguageSpec
from codebase_rag.parsers import structure_processor
from codebase_rag.parsers.structure_processor import StructureProcessor
from codebase_rag.tests.conftest import create_and_run_updater, get_node_names


def _make_mock_queries(
    package_indicators: tuple[str, ...],
) -> dict[str, MagicMock | LanguageSpec | None]:
    return {
        "functions": None,
        "classes": None,
        "calls": None,
        "imports": None,
        "locals": None,
        "config": LanguageSpec(
            language=SupportedLanguage.PYTHON,
            file_extensions=(".py",),
            function_node_types=(),
            class_node_types=(),
            module_node_types=(),
            package_indicators=package_indicators,
        ),
        "language": MagicMock(),
        "parser": MagicMock(),
    }


@pytest.fixture
def mock_language_queries() -> dict[
    SupportedLanguage, dict[str, MagicMock | LanguageSpec | None]
]:
    return {SupportedLanguage.PYTHON: _make_mock_queries(("__init__.py",))}


@pytest.fixture
def processor(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    mock_language_queries: dict[
        SupportedLanguage, dict[str, MagicMock | LanguageSpec | None]
    ],
) -> StructureProcessor:
    return StructureProcessor(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        project_name="test_project",
        queries=mock_language_queries,
    )


class TestIdentifyStructure:
    def test_empty_repo_creates_no_nodes(
        self, processor: StructureProcessor, mock_ingestor: MagicMock
    ) -> None:
        processor.identify_structure()
        mock_ingestor.ensure_node_batch.assert_not_called()

    def test_directory_with_init_py_identified_as_package(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        pkg_dir = temp_repo / "mypackage"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").touch()

        processor.identify_structure()

        package_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Package"
        ]
        assert len(package_calls) == 1
        props = package_calls[0][0][1]
        assert props["qualified_name"] == "test_project.mypackage"
        assert props["name"] == "mypackage"
        assert props["path"] == "mypackage"

    def test_directory_without_init_py_identified_as_folder(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        folder_dir = temp_repo / "myfolder"
        folder_dir.mkdir()

        processor.identify_structure()

        folder_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Folder"
        ]
        assert len(folder_calls) == 1
        props = folder_calls[0][0][1]
        assert props["path"] == "myfolder"
        assert props["name"] == "myfolder"

    def test_nested_packages(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        parent_pkg = temp_repo / "parent"
        parent_pkg.mkdir()
        (parent_pkg / "__init__.py").touch()

        child_pkg = parent_pkg / "child"
        child_pkg.mkdir()
        (child_pkg / "__init__.py").touch()

        processor.identify_structure()

        package_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Package"
        ]
        qualified_names = {c[0][1]["qualified_name"] for c in package_calls}
        assert qualified_names == {"test_project.parent", "test_project.parent.child"}

    def test_package_inside_folder(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        folder = temp_repo / "folder"
        folder.mkdir()

        pkg = folder / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()

        processor.identify_structure()

        folder_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Folder"
        ]
        package_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Package"
        ]

        assert len(folder_calls) == 1
        assert folder_calls[0][0][1]["path"] == "folder"

        assert len(package_calls) == 1
        assert package_calls[0][0][1]["qualified_name"] == "test_project.folder.pkg"

    def test_folder_inside_package(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        pkg = temp_repo / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()

        folder = pkg / "subfolder"
        folder.mkdir()

        processor.identify_structure()

        package_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Package"
        ]
        folder_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Folder"
        ]

        assert len(package_calls) == 1
        assert package_calls[0][0][1]["qualified_name"] == "test_project.pkg"

        assert len(folder_calls) == 1
        assert folder_calls[0][0][1]["path"] == "pkg/subfolder"

    def test_ignored_directories_are_skipped(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        git_dir = temp_repo / ".git"
        git_dir.mkdir()

        pycache_dir = temp_repo / "__pycache__"
        pycache_dir.mkdir()

        venv_dir = temp_repo / "venv"
        venv_dir.mkdir()

        valid_dir = temp_repo / "valid"
        valid_dir.mkdir()

        processor.identify_structure()

        folder_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Folder"
        ]
        folder_paths = {c[0][1]["path"] for c in folder_calls}

        assert "valid" in folder_paths
        assert ".git" not in folder_paths
        assert "__pycache__" not in folder_paths
        assert "venv" not in folder_paths

    def test_nested_ignored_directory_skipped(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        pkg = temp_repo / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()

        pycache = pkg / "__pycache__"
        pycache.mkdir()

        processor.identify_structure()

        folder_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Folder"
        ]
        folder_paths = {c[0][1]["path"] for c in folder_calls}
        assert "pkg/__pycache__" not in folder_paths

    def test_package_parent_relationship_to_project(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        pkg = temp_repo / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()

        processor.identify_structure()

        rel_calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == "CONTAINS_PACKAGE"
        ]
        assert len(rel_calls) == 1
        from_spec, rel_type, to_spec = rel_calls[0][0]
        assert from_spec == ("Project", "name", "test_project")
        assert rel_type == "CONTAINS_PACKAGE"
        assert to_spec == ("Package", "qualified_name", "test_project.pkg")

    def test_nested_package_parent_relationship(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        parent = temp_repo / "parent"
        parent.mkdir()
        (parent / "__init__.py").touch()

        child = parent / "child"
        child.mkdir()
        (child / "__init__.py").touch()

        processor.identify_structure()

        rel_calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == "CONTAINS_PACKAGE"
        ]
        assert len(rel_calls) == 2

        child_rel = next(
            c for c in rel_calls if c[0][2][2] == "test_project.parent.child"
        )
        from_spec, _, to_spec = child_rel[0]
        assert from_spec == ("Package", "qualified_name", "test_project.parent")

    def test_folder_parent_relationship_to_project(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        folder = temp_repo / "folder"
        folder.mkdir()

        processor.identify_structure()

        rel_calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == "CONTAINS_FOLDER"
        ]
        assert len(rel_calls) == 1
        from_spec, rel_type, to_spec = rel_calls[0][0]
        assert from_spec == ("Project", "name", "test_project")
        assert rel_type == "CONTAINS_FOLDER"
        assert to_spec == (
            "Folder",
            "absolute_path",
            (temp_repo / "folder").resolve().as_posix(),
        )

    def test_structural_elements_populated(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
    ) -> None:
        pkg = temp_repo / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()

        folder = temp_repo / "folder"
        folder.mkdir()

        processor.identify_structure()

        assert processor.structural_elements[Path("pkg")] == "test_project.pkg"
        assert processor.structural_elements[Path("folder")] is None

    def test_a_skipped_directory_is_not_entered(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # CodeRabbit review of #3644: `rglob` listed all of `.git/objects`
        # only to reject each entry; the walk now prunes before descending.
        for folder in ("objects/12", "objects/34", "refs/heads"):
            (temp_repo / ".git" / folder).mkdir(parents=True)
        (temp_repo / "valid" / "inner").mkdir(parents=True)
        asked: list[str] = []
        real = structure_processor.should_skip_path

        def spy(
            path: Path, repo_path: Path, **kwargs: frozenset[str] | bool | None
        ) -> bool:
            asked.append(path.relative_to(temp_repo).as_posix())
            return real(path, repo_path, **kwargs)

        monkeypatch.setattr(structure_processor, "should_skip_path", spy)
        processor.identify_structure(emit=False)

        assert ".git" in asked
        assert not [path for path in asked if path.startswith(".git/")], asked
        assert Path("valid/inner") in processor.structural_elements

    def test_a_scoped_walk_reaches_a_directory_under_an_unnamed_one(
        self, temp_repo: Path, processor: StructureProcessor
    ) -> None:
        (temp_repo / "a" / "b" / "c").mkdir(parents=True)

        processor.identify_structure(only={"a/b"}, emit=False)

        assert Path("a/b") in processor.structural_elements
        assert Path("a") not in processor.structural_elements
        assert Path("a/b/c") not in processor.structural_elements

    @pytest.mark.skipif(
        sys.platform == cs.PLATFORM_WINDOWS or os.geteuid() == 0,
        reason="needs POSIX permissions that bind the user running the tests",
    )
    def test_a_folder_the_walk_may_not_enter_is_not_a_package(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Issue #3748: the parent lists the folder but nothing in it can be
        # stat'ed, and on Python 3.12 and 3.13 asking for its `__init__.py`
        # raised PermissionError out of the whole run. Python 3.14 reads it
        # as absent, so the folder is a Folder on every version. Three levels
        # down because the TypeScript alias search stats `tsconfig.json` in
        # the first two on its own, before the structure pass runs.
        package = temp_repo / "pkg" / "sub"
        locked = package / "locked"
        locked.mkdir(parents=True)
        for directory in (package.parent, package, locked):
            (directory / "__init__.py").touch()
        (package / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            updater = create_and_run_updater(
                temp_repo, mock_ingestor, skip_if_missing="python"
            )
            is_package = updater.factory.structure_processor.is_package_dir(locked)
        finally:
            locked.chmod(0o755)

        assert is_package is False
        recorded = {
            (c.args[0], c.args[1].get("path"))
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c.args[0] in ("Package", "Folder")
        }
        assert recorded == {
            ("Package", "pkg"),
            ("Package", "pkg/sub"),
            ("Folder", "pkg/sub/locked"),
        }
        modules = get_node_names(mock_ingestor, "Module")
        assert any(name.endswith(".pkg.sub.mod") for name in modules), modules


class TestProcessGenericFile:
    def test_file_in_package(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        pkg = temp_repo / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").touch()

        file_path = pkg / "data.json"
        file_path.touch()

        processor.identify_structure()
        mock_ingestor.reset_mock()

        processor.process_generic_file(file_path, "data.json")

        node_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "File"
        ]
        assert len(node_calls) == 1
        props = node_calls[0][0][1]
        assert props["path"] == "pkg/data.json"
        assert props["name"] == "data.json"
        assert props["extension"] == ".json"

        rel_calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == "CONTAINS_FILE"
        ]
        assert len(rel_calls) == 1
        from_spec, _, to_spec = rel_calls[0][0]
        assert from_spec == ("Package", "qualified_name", "test_project.pkg")
        assert to_spec == (
            "File",
            "absolute_path",
            (temp_repo / "pkg" / "data.json").resolve().as_posix(),
        )

    def test_symlinked_file_keyed_on_link_not_target(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        target = temp_repo / "real.md"
        target.write_text("payload")
        link = temp_repo / "alias.md"
        link.symlink_to(target)

        processor.process_generic_file(link, "alias.md")

        node_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "File"
        ]
        assert len(node_calls) == 1
        stored = node_calls[0][0][1]["absolute_path"]

        expected = (temp_repo.resolve() / "alias.md").as_posix()
        assert stored == expected
        assert stored != (temp_repo.resolve() / "real.md").as_posix()

        link.unlink()
        assert link.resolve().as_posix() == stored

    def test_file_in_folder(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        folder = temp_repo / "folder"
        folder.mkdir()

        file_path = folder / "readme.txt"
        file_path.touch()

        processor.identify_structure()
        mock_ingestor.reset_mock()

        processor.process_generic_file(file_path, "readme.txt")

        rel_calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == "CONTAINS_FILE"
        ]
        assert len(rel_calls) == 1
        from_spec, _, _ = rel_calls[0][0]
        assert from_spec == (
            "Folder",
            "absolute_path",
            (temp_repo / "folder").resolve().as_posix(),
        )

    def test_file_at_root(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        file_path = temp_repo / "config.yaml"
        file_path.touch()

        processor.identify_structure()
        mock_ingestor.reset_mock()

        processor.process_generic_file(file_path, "config.yaml")

        node_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "File"
        ]
        assert len(node_calls) == 1
        props = node_calls[0][0][1]
        assert props["path"] == "config.yaml"

        rel_calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == "CONTAINS_FILE"
        ]
        assert len(rel_calls) == 1
        from_spec, _, _ = rel_calls[0][0]
        assert from_spec == ("Project", "name", "test_project")

    def test_file_extension_extracted(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        file_path = temp_repo / "script.sh"
        file_path.touch()

        processor.process_generic_file(file_path, "script.sh")

        node_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "File"
        ]
        assert node_calls[0][0][1]["extension"] == ".sh"

    def test_file_without_extension(
        self,
        temp_repo: Path,
        processor: StructureProcessor,
        mock_ingestor: MagicMock,
    ) -> None:
        file_path = temp_repo / "Makefile"
        file_path.touch()

        processor.process_generic_file(file_path, "Makefile")

        node_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "File"
        ]
        assert node_calls[0][0][1]["extension"] == ""


class TestMultipleLanguages:
    def test_multiple_package_indicators(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        queries = {
            SupportedLanguage.PYTHON: _make_mock_queries(("__init__.py",)),
            SupportedLanguage.RUST: _make_mock_queries(("Cargo.toml",)),
        }

        processor = StructureProcessor(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            project_name="multi_lang",
            queries=queries,
        )

        py_pkg = temp_repo / "pypkg"
        py_pkg.mkdir()
        (py_pkg / "__init__.py").touch()

        rust_pkg = temp_repo / "rustpkg"
        rust_pkg.mkdir()
        (rust_pkg / "Cargo.toml").touch()

        processor.identify_structure()

        package_calls = [
            c
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c[0][0] == "Package"
        ]
        qualified_names = {c[0][1]["qualified_name"] for c in package_calls}
        assert qualified_names == {"multi_lang.pypkg", "multi_lang.rustpkg"}


class TestStructureProcessorSlots:
    def test_has_slots(self) -> None:
        assert hasattr(StructureProcessor, "__slots__")

    def test_no_instance_dict(self, processor: StructureProcessor) -> None:
        assert not hasattr(processor, "__dict__")

    def test_rejects_arbitrary_attribute(self, processor: StructureProcessor) -> None:
        with pytest.raises(AttributeError):
            processor.nonexistent_attr = 42

    def test_slot_attributes_accessible(self, processor: StructureProcessor) -> None:
        assert hasattr(processor, "ingestor")
        assert hasattr(processor, "repo_path")
        assert hasattr(processor, "project_name")
        assert hasattr(processor, "queries")
        assert hasattr(processor, "structural_elements")
