# (H) Tests for orphan node pruning in GraphUpdater._prune_orphan_nodes
# (H) and Cypher deletion in _process_files for hash-cache-detected deletions.
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )


@pytest.fixture
def py_project(temp_repo: Path) -> Path:
    (temp_repo / "__init__.py").touch()
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    (temp_repo / "module_b.py").write_text("def func_b():\n    pass\n")
    sub = temp_repo / "subpkg"
    sub.mkdir()
    (sub / "__init__.py").touch()
    (sub / "inner.py").write_text("def inner_func():\n    pass\n")
    return temp_repo


class TestPruneOrphanNodes:
    def test_prune_removes_orphan_module_nodes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        project_name = py_project.resolve().name

        mock_ingestor.fetch_all.return_value = [
            {
                "path": "old_project/main.py",
                "qualified_name": f"{project_name}.old_project.main",
            },
            {
                "path": "module_a.py",
                "qualified_name": f"{project_name}.module_a",
            },
        ]
        updater._prune_orphan_nodes()

        delete_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_MODULE
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[1] == {cs.KEY_PATH: "old_project/main.py"}

    def test_prune_skips_other_projects(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.return_value = [
            {
                "path": "app.py",
                "qualified_name": "other_project.app",
            },
        ]
        updater._prune_orphan_nodes()

        assert mock_ingestor.execute_write.call_count == 0

    def test_prune_no_orphans_skips_deletes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        project_name = py_project.resolve().name
        mock_ingestor.fetch_all.return_value = [
            {
                "path": "module_a.py",
                "qualified_name": f"{project_name}.module_a",
            },
        ]
        updater._prune_orphan_nodes()

        assert mock_ingestor.execute_write.call_count == 0

    def test_prune_handles_empty_graph(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.return_value = []
        updater._prune_orphan_nodes()

        assert mock_ingestor.execute_write.call_count == 0

    def test_prune_handles_none_path_gracefully(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        project_name = py_project.resolve().name
        mock_ingestor.fetch_all.return_value = [
            {"path": None, "qualified_name": f"{project_name}.something"},
            {"path": "module_a.py", "qualified_name": f"{project_name}.module_a"},
        ]
        updater._prune_orphan_nodes()

        assert mock_ingestor.execute_write.call_count == 0

    def test_prune_multiple_orphan_modules(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        project_name = py_project.resolve().name
        mock_ingestor.fetch_all.return_value = [
            {
                "path": "deleted1.py",
                "qualified_name": f"{project_name}.deleted1",
            },
            {
                "path": "deleted2.py",
                "qualified_name": f"{project_name}.deleted2",
            },
            {
                "path": "module_a.py",
                "qualified_name": f"{project_name}.module_a",
            },
        ]
        updater._prune_orphan_nodes()

        assert mock_ingestor.execute_write.call_count == 2


class TestDeletedFileInProcessFiles:
    def test_deleted_file_triggers_cypher_delete(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        updater.run(force=True)
        mock_ingestor.execute_write.reset_mock()

        (py_project / "module_b.py").unlink()
        updater.run(force=False)

        delete_module_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_MODULE
        ]
        delete_file_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert len(delete_module_calls) >= 1
        assert len(delete_file_calls) >= 1

    def test_no_deletes_when_no_files_removed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        updater.run(force=True)
        mock_ingestor.execute_write.reset_mock()

        updater.run(force=False)

        delete_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] in (cs.CYPHER_DELETE_MODULE, cs.CYPHER_DELETE_FILE)
        ]
        assert len(delete_calls) == 0

    @patch("codebase_rag.graph_updater.GraphUpdater._prune_orphan_nodes")
    def test_run_calls_prune(
        self,
        mock_prune: MagicMock,
        py_project: Path,
        mock_ingestor: MagicMock,
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        updater.run(force=True)
        mock_prune.assert_called_once()
