from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_node_names


@pytest.fixture(scope="module")
def parsers_and_queries() -> tuple[dict, dict]:
    return load_parsers()


def _make_updater(
    repo_path: Path,
    mock_ingestor: MagicMock,
    parsers_and_queries: tuple[dict, dict],
    project_name: str | None = None,
) -> GraphUpdater:
    parsers, queries = parsers_and_queries
    return GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
    )


def _write_python_file(repo_path: Path, rel_path: str, content: str) -> None:
    full = repo_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


class TestDefaultProjectName:
    def test_default_uses_directory_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(temp_repo, mock_ingestor, parsers_and_queries)
        assert updater.project_name == temp_repo.resolve().name

    def test_default_none_uses_directory_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name=None
        )
        assert updater.project_name == temp_repo.resolve().name

    def test_default_empty_string_uses_directory_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name=""
        )
        assert updater.project_name == temp_repo.resolve().name

    def test_default_whitespace_only_uses_directory_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="   "
        )
        assert updater.project_name == temp_repo.resolve().name


class TestExplicitProjectName:
    def test_override_simple(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="MyProject"
        )
        assert updater.project_name == "MyProject"

    def test_override_with_hyphens(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo,
            mock_ingestor,
            parsers_and_queries,
            project_name="my-cool-project",
        )
        assert updater.project_name == "my-cool-project"

    def test_override_with_dots(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo,
            mock_ingestor,
            parsers_and_queries,
            project_name="com.example.app",
        )
        assert updater.project_name == "com.example.app"


class TestEdgeCases:
    def test_generic_dir_name_src(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        src_dir = temp_repo / "src"
        src_dir.mkdir()
        updater = _make_updater(
            src_dir, mock_ingestor, parsers_and_queries, project_name="BlazingRenderer"
        )
        assert updater.project_name == "BlazingRenderer"
        updater_default = _make_updater(src_dir, mock_ingestor, parsers_and_queries)
        assert updater_default.project_name == "src"

    def test_generic_dir_name_main(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        main_dir = temp_repo / "main"
        main_dir.mkdir()
        updater = _make_updater(
            main_dir,
            mock_ingestor,
            parsers_and_queries,
            project_name="ActualProjectName",
        )
        assert updater.project_name == "ActualProjectName"

    def test_version_named_directory(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        ver_dir = temp_repo / "v1.3.2"
        ver_dir.mkdir()
        updater = _make_updater(
            ver_dir, mock_ingestor, parsers_and_queries, project_name="my-library"
        )
        assert updater.project_name == "my-library"
        updater_default = _make_updater(ver_dir, mock_ingestor, parsers_and_queries)
        assert updater_default.project_name == "v1.3.2"

    def test_nested_same_name_parent(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        nested = temp_repo / "BRender" / "BlazingRenderer"
        nested.mkdir(parents=True)
        updater = _make_updater(
            nested, mock_ingestor, parsers_and_queries, project_name="BlazingRenderer"
        )
        assert updater.project_name == "BlazingRenderer"


class TestFactoryPropagation:
    def test_factory_receives_project_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="CustomName"
        )
        assert updater.factory.project_name == "CustomName"

    def test_factory_default_project_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(temp_repo, mock_ingestor, parsers_and_queries)
        assert updater.factory.project_name == temp_repo.resolve().name

    def test_structure_processor_receives_project_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="CustomName"
        )
        assert updater.factory.structure_processor.project_name == "CustomName"

    def test_import_processor_receives_project_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="CustomName"
        )
        assert updater.factory.import_processor.project_name == "CustomName"

    def test_definition_processor_receives_project_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="CustomName"
        )
        assert updater.factory.definition_processor.project_name == "CustomName"

    def test_call_processor_receives_project_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="CustomName"
        )
        assert updater.factory.call_processor.project_name == "CustomName"

    def test_type_inference_receives_project_name(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="CustomName"
        )
        assert updater.factory.type_inference.project_name == "CustomName"


class TestQualifiedNameIntegration:
    def test_module_qualified_names_use_override(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        _write_python_file(temp_repo, "hello.py", "def greet():\n    pass\n")
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="MyApp"
        )
        updater.run(force=True)
        module_names = get_node_names(mock_ingestor, "Module")
        assert "MyApp.hello" in module_names

    def test_function_qualified_names_use_override(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        _write_python_file(temp_repo, "utils.py", "def helper():\n    return 42\n")
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="MyApp"
        )
        updater.run(force=True)
        func_names = get_node_names(mock_ingestor, "Function")
        assert "MyApp.utils.helper" in func_names

    def test_class_qualified_names_use_override(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        _write_python_file(temp_repo, "models.py", "class User:\n    pass\n")
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="MyApp"
        )
        updater.run(force=True)
        class_names = get_node_names(mock_ingestor, "Class")
        assert "MyApp.models.User" in class_names

    def test_default_qualified_names_use_directory(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        _write_python_file(temp_repo, "foo.py", "def bar():\n    pass\n")
        updater = _make_updater(temp_repo, mock_ingestor, parsers_and_queries)
        updater.run(force=True)
        dir_name = temp_repo.resolve().name
        func_names = get_node_names(mock_ingestor, "Function")
        assert f"{dir_name}.foo.bar" in func_names

    def test_package_qualified_names_use_override(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        _write_python_file(temp_repo, "pkg/__init__.py", "")
        _write_python_file(temp_repo, "pkg/core.py", "def run():\n    pass\n")
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="CustomProj"
        )
        updater.run(force=True)
        func_names = get_node_names(mock_ingestor, "Function")
        assert "CustomProj.pkg.core.run" in func_names

    def test_override_vs_default_different_names(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        parsers_and_queries: tuple[dict, dict],
    ) -> None:
        _write_python_file(temp_repo, "app.py", "def main():\n    pass\n")
        dir_name = temp_repo.resolve().name
        updater = _make_updater(
            temp_repo, mock_ingestor, parsers_and_queries, project_name="OverrideName"
        )
        updater.run(force=True)
        func_names = get_node_names(mock_ingestor, "Function")
        assert "OverrideName.app.main" in func_names
        assert f"{dir_name}.app.main" not in func_names
