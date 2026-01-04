from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import SupportedLanguage
from codebase_rag.models import LanguageSpec
from codebase_rag.parsers.structure_processor import StructureProcessor


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
        exclude_patterns=frozenset({".git", "__pycache__", "venv"}),
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
        assert to_spec == ("Folder", "path", "folder")

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
        assert to_spec == ("File", "path", "pkg/data.json")

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
        assert from_spec == ("Folder", "path", "folder")

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
