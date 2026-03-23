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
    """Tests for GraphUpdater._prune_orphan_nodes."""

    def test_prune_removes_orphan_file_nodes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Orphan File nodes whose paths don't exist on disk are deleted."""
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        # (H) Simulate graph returning a file path that no longer exists
        mock_ingestor.fetch_all.side_effect = [
            [{"path": "deleted_project/server.py"}, {"path": "module_a.py"}],
            [],
            [],
        ]
        updater._prune_orphan_nodes()

        # (H) Only the orphan path should be deleted
        delete_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[1] == {cs.KEY_PATH: "deleted_project/server.py"}

    def test_prune_removes_orphan_module_nodes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Orphan Module nodes are deleted via CYPHER_DELETE_MODULE (cascading)."""
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.side_effect = [
            [],
            [{"path": "old_project/main.py"}],
            [],
        ]
        updater._prune_orphan_nodes()

        delete_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_MODULE
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[1] == {cs.KEY_PATH: "old_project/main.py"}

    def test_prune_removes_orphan_folder_nodes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Orphan Folder nodes are deleted via CYPHER_DELETE_FOLDER."""
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.side_effect = [
            [],
            [],
            [{"path": "projects/mcp-openclaw-bridge"}, {"path": "subpkg"}],
        ]
        updater._prune_orphan_nodes()

        delete_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FOLDER
        ]
        # (H) Only the non-existent path is pruned; "subpkg" still exists on disk
        assert len(delete_calls) == 1
        assert delete_calls[0].args[1] == {cs.KEY_PATH: "projects/mcp-openclaw-bridge"}

    def test_prune_no_orphans_skips_deletes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """When all graph nodes exist on disk, no delete queries are issued."""
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.side_effect = [
            [{"path": "module_a.py"}],
            [{"path": "module_a.py"}],
            [{"path": "subpkg"}],
        ]
        updater._prune_orphan_nodes()

        assert mock_ingestor.execute_write.call_count == 0

    def test_prune_handles_empty_graph(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Pruning on an empty graph does nothing."""
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
        """Rows with None path values are skipped without error."""
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.side_effect = [
            [{"path": None}, {"path": "module_a.py"}],
            [],
            [],
        ]
        updater._prune_orphan_nodes()

        assert mock_ingestor.execute_write.call_count == 0

    def test_prune_multiple_orphans_across_types(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Multiple orphan nodes across File, Module, Folder are all pruned."""
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.side_effect = [
            [{"path": "gone/a.py"}, {"path": "gone/b.py"}],
            [{"path": "gone/a.py"}],
            [{"path": "gone"}],
        ]
        updater._prune_orphan_nodes()

        # (H) 2 File + 1 Module + 1 Folder = 4 deletes
        assert mock_ingestor.execute_write.call_count == 4


class TestProcessFilesDeletesCypherNodes:
    """Tests that _process_files issues Cypher deletes for hash-cache-detected deletions."""

    def test_deleted_file_triggers_cypher_delete(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """When a file is deleted between runs, both MODULE and FILE Cypher deletes are issued."""
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        # (H) Stub fetch_all so _prune_orphan_nodes doesn't interfere
        mock_ingestor.fetch_all.return_value = []
        updater.run()

        (py_project / "module_b.py").unlink()
        mock_ingestor.reset_mock()
        mock_ingestor.fetch_all.return_value = []

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater2.run()

        # (H) Verify CYPHER_DELETE_MODULE and CYPHER_DELETE_FILE were called for module_b.py
        module_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_MODULE
            and c.args[1].get(cs.KEY_PATH) == "module_b.py"
        ]
        file_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
            and c.args[1].get(cs.KEY_PATH) == "module_b.py"
        ]
        assert len(module_deletes) >= 1
        assert len(file_deletes) >= 1

    def test_no_deletes_when_no_files_removed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """When no files are deleted between runs, no delete queries are issued for files."""
        parsers, queries = load_parsers()

        mock_ingestor.fetch_all.return_value = []

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        mock_ingestor.reset_mock()
        mock_ingestor.fetch_all.return_value = []

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater2.run()

        # (H) No CYPHER_DELETE_MODULE or CYPHER_DELETE_FILE for specific paths
        path_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] in (cs.CYPHER_DELETE_MODULE, cs.CYPHER_DELETE_FILE)
            and len(c.args) > 1
        ]
        assert len(path_deletes) == 0


class TestPruneCalledDuringRun:
    """Tests that _prune_orphan_nodes is called as part of GraphUpdater.run()."""

    def test_run_calls_prune(self, py_project: Path, mock_ingestor: MagicMock) -> None:
        """GraphUpdater.run() invokes _prune_orphan_nodes after flush."""
        parsers, queries = load_parsers()
        mock_ingestor.fetch_all.return_value = []

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater, "_prune_orphan_nodes", wraps=updater._prune_orphan_nodes
        ) as spy:
            updater.run()
            spy.assert_called_once()
