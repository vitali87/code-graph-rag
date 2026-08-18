# Tests for orphan node pruning in GraphUpdater._prune_orphan_nodes
# and Cypher deletion in _process_files for hash-cache-detected deletions.
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

        mock_ingestor.fetch_all.side_effect = [
            [],
            [
                {
                    "path": "old_project/main.py",
                    "qualified_name": f"{project_name}.old_project.main",
                },
                {
                    "path": "module_a.py",
                    "qualified_name": f"{project_name}.module_a",
                },
            ],
            [],
            [],
        ]
        updater._prune_orphan_nodes()

        delete_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_MODULE
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[1] == {
            cs.KEY_PATH: "old_project/main.py",
            cs.KEY_PROJECT_NAME: project_name,
            cs.KEY_PROJECT_PREFIX: f"{project_name}.",
        }

    def test_prune_removes_orphan_external_module_nodes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.side_effect = [[], [], [], []]
        updater._prune_orphan_nodes()

        external_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES
        ]
        assert len(external_calls) == 1

    def test_prune_removes_unanchored_resource_nodes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # A rebuild that drops a route or literal URL leaves its shared
        # (prefix-less) Resource node behind with no anchoring code edge.
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        mock_ingestor.fetch_all.side_effect = [[], [], [], []]
        with patch(
            "codebase_rag.graph_updater.prune_unanchored_resources"
        ) as prune_resources:
            updater._prune_orphan_nodes()

        prune_resources.assert_called_once_with(mock_ingestor)

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

        mock_ingestor.fetch_all.side_effect = [
            [{"path": "app.py", "absolute_path": "/other/project/app.py"}],
            [{"path": "app.py", "qualified_name": "other_project.app"}],
            [{"path": "data", "absolute_path": "/other/project/data"}],
            [],
        ]
        updater._prune_orphan_nodes()

        path_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0]
            in (cs.CYPHER_DELETE_FILE, cs.CYPHER_DELETE_MODULE, cs.CYPHER_DELETE_FOLDER)
        ]
        assert path_deletes == []

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
        repo_abs = py_project.resolve().as_posix()
        mock_ingestor.fetch_all.side_effect = [
            [{"path": "module_a.py", "absolute_path": f"{repo_abs}/module_a.py"}],
            [{"path": "module_a.py", "qualified_name": f"{project_name}.module_a"}],
            [{"path": "subpkg", "absolute_path": f"{repo_abs}/subpkg"}],
            [],
        ]
        updater._prune_orphan_nodes()

        path_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0]
            in (cs.CYPHER_DELETE_FILE, cs.CYPHER_DELETE_MODULE, cs.CYPHER_DELETE_FOLDER)
        ]
        assert path_deletes == []

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

        mock_ingestor.fetch_all.side_effect = [[], [], [], []]
        updater._prune_orphan_nodes()

        path_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0]
            in (cs.CYPHER_DELETE_FILE, cs.CYPHER_DELETE_MODULE, cs.CYPHER_DELETE_FOLDER)
        ]
        assert path_deletes == []

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
        mock_ingestor.fetch_all.side_effect = [
            [{"path": None, "absolute_path": None}],
            [
                {"path": None, "qualified_name": f"{project_name}.something"},
                {"path": "module_a.py", "qualified_name": f"{project_name}.module_a"},
            ],
            [],
            [],
        ]
        updater._prune_orphan_nodes()

        path_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0]
            in (cs.CYPHER_DELETE_FILE, cs.CYPHER_DELETE_MODULE, cs.CYPHER_DELETE_FOLDER)
        ]
        assert path_deletes == []

    def test_prune_multiple_orphans_across_types(
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
        repo_abs = py_project.resolve().as_posix()
        mock_ingestor.fetch_all.side_effect = [
            [
                {"path": "gone.py", "absolute_path": f"{repo_abs}/gone.py"},
                {"path": "module_a.py", "absolute_path": f"{repo_abs}/module_a.py"},
            ],
            [
                {
                    "path": "deleted.py",
                    "qualified_name": f"{project_name}.deleted",
                },
                {
                    "path": "module_a.py",
                    "qualified_name": f"{project_name}.module_a",
                },
            ],
            [
                {"path": "old_dir", "absolute_path": f"{repo_abs}/old_dir"},
                {"path": "subpkg", "absolute_path": f"{repo_abs}/subpkg"},
            ],
            [],
        ]
        updater._prune_orphan_nodes()

        path_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0]
            in (cs.CYPHER_DELETE_FILE, cs.CYPHER_DELETE_MODULE, cs.CYPHER_DELETE_FOLDER)
        ]
        assert len(path_deletes) == 3

    def test_prune_skips_inline_module_synthetic_paths(
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
        inline_path_tests = f"{cs.INLINE_MODULE_PATH_PREFIX}tests"
        inline_path_macos = f"{cs.INLINE_MODULE_PATH_PREFIX}macos"
        mock_ingestor.fetch_all.side_effect = [
            [],
            [
                {
                    "path": inline_path_tests,
                    "qualified_name": f"{project_name}.src.app.tests",
                },
                {
                    "path": inline_path_tests,
                    "qualified_name": f"{project_name}.src.cli.tests",
                },
                {
                    "path": inline_path_macos,
                    "qualified_name": f"{project_name}.src.clipboard.macos",
                },
            ],
            [],
            [],
        ]
        updater._prune_orphan_nodes()

        delete_module_calls = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_MODULE
        ]
        assert delete_module_calls == []


class TestCypherDeleteModuleQuery:
    def test_query_does_not_traverse_calls_edges(self) -> None:
        query = cs.CYPHER_DELETE_MODULE
        assert "-[*0..]->" not in query
        assert "-[*]->" not in query

    def test_query_constrains_traversal_to_containment_edges(self) -> None:
        query = cs.CYPHER_DELETE_MODULE
        assert "DEFINES" in query
        assert "CALLS" not in query
        assert "IMPORTS" not in query
        assert "INHERITS" not in query


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


class TestPruneSiblingRootPrefix:
    """A sibling repo root sharing a string prefix must not be pruned (#897)."""

    def _updater(self, py_project: Path, mock_ingestor: MagicMock) -> GraphUpdater:
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

    def test_prune_skips_sibling_root_sharing_path_prefix(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        updater = self._updater(py_project, mock_ingestor)
        repo_abs = py_project.resolve().as_posix()
        sibling_abs = f"{repo_abs}-old/app.py"

        mock_ingestor.fetch_all.side_effect = [
            [{"path": "app.py", "absolute_path": sibling_abs}],
            [],
            [],
            [],
        ]
        updater._prune_orphan_nodes()

        file_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert file_deletes == []

    def test_prune_still_sweeps_own_missing_file(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        updater = self._updater(py_project, mock_ingestor)
        own_abs = (py_project / "gone.py").resolve().as_posix()

        mock_ingestor.fetch_all.side_effect = [
            [{"path": "gone.py", "absolute_path": own_abs}],
            [],
            [],
            [],
        ]
        updater._prune_orphan_nodes()

        file_deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert len(file_deletes) == 1
        assert file_deletes[0].args[1] == {cs.KEY_PATH: own_abs}


class TestLegacyFileIdentitySweep:
    # Issue #1156: a pre-GHSA-85gg graph can hold a File node keyed on an
    # external symlink's DEREFERENCED target. Its absolute_path sits outside
    # the repo, so the containment gate skips it and it is never swept. The
    # sweep drops such nodes when the stored key disagrees with the identity
    # derivable from the relative path, unless another project owns the key
    # (File nodes MERGE globally on absolute_path).

    def _run_prune(self, updater, mock_ingestor, file_rows, owners_by_key):
        owner_calls: list[str] = []

        def fetch_all(query, params=None):
            if query == cs.CYPHER_ALL_FILE_PATHS:
                return file_rows
            if query == cs.CYPHER_FILE_CONTAINERS:
                owner_calls.append(params[cs.KEY_PATH])
                return owners_by_key.get(params[cs.KEY_PATH], [])
            return []

        mock_ingestor.fetch_all.side_effect = fetch_all
        updater._prune_orphan_nodes()
        return owner_calls

    def test_legacy_external_file_identity_is_swept(
        self, updater: GraphUpdater, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        row = {"path": "cfg/link.yaml", "absolute_path": "/outside/target.yaml"}
        owners = {
            "/outside/target.yaml": [
                {
                    "labels": ["Folder"],
                    "name": None,
                    "absolute_path": (temp_repo / "cfg").resolve().as_posix(),
                }
            ]
        }
        self._run_prune(updater, mock_ingestor, [row], owners)
        deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert len(deletes) == 1
        assert deletes[0].args[1] == {cs.KEY_PATH: "/outside/target.yaml"}

    @pytest.mark.parametrize(
        "owner",
        [
            {"labels": ["Folder"], "name": None, "absolute_path": "/other/repo/cfg"},
            {"labels": ["Project"], "name": "other_project", "absolute_path": None},
        ],
    )
    def test_legacy_sweep_spares_a_foreign_owned_key(
        self, updater: GraphUpdater, mock_ingestor: MagicMock, owner: dict
    ) -> None:
        # The key may be the SAME node another project legitimately owns:
        # deleting it would be cross-project data loss, so any foreign
        # container vetoes the sweep.
        row = {"path": "cfg/link.yaml", "absolute_path": "/other/repo/cfg/real.yaml"}
        owners = {"/other/repo/cfg/real.yaml": [owner]}
        self._run_prune(updater, mock_ingestor, [row], owners)
        deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert deletes == []

    def test_legacy_sweep_requires_positive_ownership(
        self, updater: GraphUpdater, mock_ingestor: MagicMock
    ) -> None:
        # A key with no visible containers offers no evidence of sole
        # ownership (edges may be missing or unreadable); the sweep must
        # spare it rather than guess.
        row = {"path": "cfg/link.yaml", "absolute_path": "/outside/target.yaml"}
        self._run_prune(updater, mock_ingestor, [row], {})
        deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert deletes == []

    def test_symlinked_ancestor_identity_is_not_a_sweep_candidate(
        self,
        updater: GraphUpdater,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        tmp_path: Path,
    ) -> None:
        # A file under an in-repo DIRECTORY symlink legitimately keys outside
        # the repo under the current scheme (parent resolved, leaf kept); its
        # stored key matches the derivable identity and must never be swept.
        outside = tmp_path / "outside_pkg"
        outside.mkdir()
        (outside / "x.py").write_text("A = 1\n", encoding="utf-8")
        (temp_repo / "sub").symlink_to(outside, target_is_directory=True)
        from codebase_rag.utils.path_utils import cached_file_identity_posix

        stored = cached_file_identity_posix(temp_repo / "sub" / "x.py")
        row = {"path": "sub/x.py", "absolute_path": stored}
        owner_calls = self._run_prune(updater, mock_ingestor, [row], {})
        deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert deletes == []
        assert owner_calls == []

    def test_containers_query_matches_the_real_containment_type(self) -> None:
        # File parents link via CONTAINS_FILE (structure_processor); a wrong
        # relationship type would silently return no owners and neuter the
        # sweep behind the positive-attribution rule.
        assert cs.RelationshipType.CONTAINS_FILE.value in cs.CYPHER_FILE_CONTAINERS

    def test_legacy_sweep_spares_a_key_with_an_unidentifiable_container(
        self, updater: GraphUpdater, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # One local owner plus one container with no usable identity: the
        # unknown row could be a foreign project, so the sweep must spare.
        row = {"path": "cfg/link.yaml", "absolute_path": "/outside/target.yaml"}
        owners = {
            "/outside/target.yaml": [
                {
                    "labels": ["Folder"],
                    "name": None,
                    "absolute_path": (temp_repo / "cfg").resolve().as_posix(),
                },
                {"labels": ["Folder"], "name": None, "absolute_path": None},
            ]
        }
        self._run_prune(updater, mock_ingestor, [row], owners)
        deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert deletes == []

    def test_legacy_sweep_survives_an_ownership_read_failure(
        self, updater: GraphUpdater, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The per-candidate read runs after the ordinary orphan deletes; a
        # raise must neither delete the key nor escape the prune.
        row = {"path": "cfg/link.yaml", "absolute_path": "/outside/target.yaml"}

        def fetch_all(query, params=None):
            if query == cs.CYPHER_ALL_FILE_PATHS:
                return [row]
            if query == cs.CYPHER_FILE_CONTAINERS:
                raise RuntimeError("connection dropped")
            return []

        mock_ingestor.fetch_all.side_effect = fetch_all
        updater._prune_orphan_nodes()
        deletes = [
            c
            for c in mock_ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert deletes == []
