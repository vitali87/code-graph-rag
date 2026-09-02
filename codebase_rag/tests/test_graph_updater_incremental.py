import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import (
    BoundedASTCache,
    FunctionRegistryTrie,
    GraphUpdater,
    _hash_file,
    _hash_file_with_bytes,
    _load_hash_cache,
    _save_hash_cache,
)
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


_EXCLUDED_QNS = frozenset(
    {
        "excludable.module_a",
        "excludable.module_a.Alpha",
        "excludable.module_a.Alpha.greet",
    }
)


def _emitted_qns(mock_ingestor: MagicMock) -> set[str]:
    return {
        call.args[1]["qualified_name"]
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if len(call.args) > 1 and "qualified_name" in call.args[1]
    }


def _module_path_queries(mock_ingestor: MagicMock) -> int:
    """How many times this run asked the graph for its project module paths."""
    return sum(
        1
        for call in mock_ingestor.fetch_all.call_args_list
        if call.args and call.args[0] == cs.CYPHER_PROJECT_MODULE_PATHS
    )


def _deleted_module_paths(mock_ingestor: MagicMock) -> set[str]:
    return {
        call.args[1][cs.KEY_PATH]
        for call in mock_ingestor.execute_write.call_args_list
        if call.args and call.args[0] == cs.CYPHER_DELETE_MODULE
    }


@pytest.fixture
def excludable_project(tmp_path: Path) -> Path:
    """A project whose module_a carries a Module, a Class and a Method.

    Named apart from `temp_repo` so the project name (and therefore every
    qualified name asserted above) is stable.
    """
    repo = tmp_path / "excludable"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "module_a.py").write_text(
        "class Alpha:\n    def greet(self):\n        return 1\n"
    )
    (repo / "module_b.py").write_text("def func_b():\n    pass\n")
    return repo


@pytest.fixture
def py_project(temp_repo: Path) -> Path:
    (temp_repo / "__init__.py").touch()
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    (temp_repo / "module_b.py").write_text("def func_b():\n    pass\n")
    return temp_repo


class TestHashFile:
    def test_hash_returns_hex_string(self, temp_repo: Path) -> None:
        f = temp_repo / "test.py"
        f.write_text("hello")
        result = _hash_file(f)
        assert isinstance(result, str)
        assert len(result) == 32

    def test_same_content_same_hash(self, temp_repo: Path) -> None:
        f1 = temp_repo / "a.py"
        f2 = temp_repo / "b.py"
        f1.write_text("same content")
        f2.write_text("same content")
        assert _hash_file(f1) == _hash_file(f2)

    def test_different_content_different_hash(self, temp_repo: Path) -> None:
        f1 = temp_repo / "a.py"
        f2 = temp_repo / "b.py"
        f1.write_text("content one")
        f2.write_text("content two")
        assert _hash_file(f1) != _hash_file(f2)

    def test_hash_with_bytes_returns_none_for_broken_symlink(
        self, temp_repo: Path
    ) -> None:
        link = temp_repo / "result"
        link.symlink_to(temp_repo / "missing-target")
        assert _hash_file_with_bytes(link) is None

    def test_hash_with_bytes_returns_none_for_missing_file(
        self, temp_repo: Path
    ) -> None:
        assert _hash_file_with_bytes(temp_repo / "does-not-exist") is None


class TestHashCacheIO:
    def test_save_and_load_cache(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        data = {"module_a.py": "abc123", "module_b.py": "def456"}
        _save_hash_cache(cache_path, data)

        assert cache_path.is_file()
        loaded = _load_hash_cache(cache_path)
        assert loaded == data

    def test_load_nonexistent_returns_empty(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        assert _load_hash_cache(cache_path) == {}

    def test_load_corrupted_returns_empty(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        cache_path.write_text("not valid json {{{")
        assert _load_hash_cache(cache_path) == {}

    def test_save_creates_parent_dirs(self, temp_repo: Path) -> None:
        cache_path = temp_repo / "subdir" / "nested" / cs.HASH_CACHE_FILENAME
        _save_hash_cache(cache_path, {"a.py": "hash1"})
        assert cache_path.is_file()

    def test_cache_file_is_valid_json(self, temp_repo: Path) -> None:
        cache_path = temp_repo / cs.HASH_CACHE_FILENAME
        data = {"file.py": "sha256hash"}
        _save_hash_cache(cache_path, data)
        with cache_path.open() as f:
            parsed = json.load(f)
        assert parsed == data


class TestIncrementalUpdates:
    def test_unchanged_file_is_skipped(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        mock_ingestor.reset_mock()
        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run()
            assert spy.call_count == 0

    def test_changed_file_is_reparsed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_a.py").write_text("def func_a_updated():\n    pass\n")

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run()
            processed_paths = [call.args[0] for call in spy.call_args_list]
            assert py_project / "module_a.py" in processed_paths

    def test_deleted_file_removed_from_state(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_b.py").unlink()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "remove_file_from_state", wraps=updater2.remove_file_from_state
        ) as spy:
            updater2.run()
            removed_paths = [call.args[0] for call in spy.call_args_list]
            assert py_project / "module_b.py" in removed_paths

    def test_force_bypasses_cache(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run(force=True)
            assert spy.call_count > 0

    def test_new_file_is_processed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_c.py").write_text("def func_c():\n    pass\n")

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(
            updater2, "_process_single_file", wraps=updater2._process_single_file
        ) as spy:
            updater2.run()
            processed_paths = [call.args[0] for call in spy.call_args_list]
            assert py_project / "module_c.py" in processed_paths

    def test_hash_cache_file_created_after_run(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        cache_path = py_project / cs.HASH_CACHE_FILENAME
        assert not cache_path.exists()

        updater.run()

        assert cache_path.is_file()
        with cache_path.open() as f:
            data = json.load(f)
        assert isinstance(data, dict)
        assert len(data) > 0

    def test_broken_symlink_does_not_crash_indexing(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        broken = py_project / "result"
        broken.symlink_to(py_project / "missing-nix-store-path")

        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )

        updater.run()

        cache_path = py_project / cs.HASH_CACHE_FILENAME
        assert cache_path.is_file()
        with cache_path.open() as f:
            data = json.load(f)
        assert "result" not in data
        assert "module_a.py" in data

    def test_deleted_file_removed_from_hash_cache(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        cache_path = py_project / cs.HASH_CACHE_FILENAME
        with cache_path.open() as f:
            old_data = json.load(f)
        assert "module_b.py" in old_data

        (py_project / "module_b.py").unlink()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater2.run()

        with cache_path.open() as f:
            new_data = json.load(f)
        assert "module_b.py" not in new_data


class TestFastPathInSync:
    def test_second_run_skips_all_passes(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert updater2._is_already_in_sync() is True
        with (
            patch.object(
                updater2, "_process_single_file", wraps=updater2._process_single_file
            ) as spy_files,
            patch.object(updater2, "_process_function_calls") as spy_calls,
        ):
            updater2.run()
            assert spy_files.call_count == 0
            assert spy_calls.call_count == 0

    def test_changed_file_disables_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_a.py").write_text("def func_a():\n    return 1\n")

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert updater2._is_already_in_sync() is False

    def test_new_file_disables_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_c.py").write_text("def func_c():\n    pass\n")

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert updater2._is_already_in_sync() is False

    def test_deleted_file_disables_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        (py_project / "module_a.py").unlink()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert updater2._is_already_in_sync() is False

    def test_no_hash_cache_disables_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert updater._is_already_in_sync() is False

    def test_changed_cli_exclusion_disables_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=frozenset({"module_a.py"}),
        )
        assert updater2._is_already_in_sync() is False

    def test_changed_unignore_disables_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
            unignore_paths=frozenset({"vendor/**"}),
        )
        assert updater2._is_already_in_sync() is False

    def test_unchanged_exclusion_keeps_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        exclusions = frozenset({"module_a.py"})
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        )
        updater.run()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        )
        assert updater2._is_already_in_sync() is True

    def test_newly_excluded_file_leaves_the_index(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()
        assert _EXCLUDED_QNS <= _emitted_qns(mock_ingestor)

        mock_ingestor.reset_mock()
        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=frozenset({"module_a.py"}),
        )
        with patch.object(
            updater2, "_process_files", wraps=updater2._process_files
        ) as spy_files:
            updater2.run()

        assert spy_files.call_count == 1
        assert "module_a.py" in _deleted_module_paths(mock_ingestor)
        # Deleting is not enough: a run that deleted and then re-parsed the
        # file would put every node straight back.
        assert not (_EXCLUDED_QNS & _emitted_qns(mock_ingestor))
        with (excludable_project / cs.HASH_CACHE_FILENAME).open() as f:
            assert "module_a.py" not in json.load(f)

    def test_cgrignore_exclusion_leaves_the_index(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Control for #1606: the path that already worked must keep working.

        A `.cgrignore` edit is caught even without an exclusion stamp,
        because that file is itself indexed and hashed, so its own hash
        turns the sync check over. The CLI merges both sources before
        constructing the updater, so the exclusion set is passed either way
        and only the on-disk change distinguishes them.
        """
        parsers, queries = load_parsers()
        ignore_file = excludable_project / ".cgrignore"
        ignore_file.write_text("\n")
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()
        assert _EXCLUDED_QNS <= _emitted_qns(mock_ingestor)

        ignore_file.write_text("module_a.py\n")
        mock_ingestor.reset_mock()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=frozenset({"module_a.py"}),
        ).run()

        assert "module_a.py" in _deleted_module_paths(mock_ingestor)
        assert not (_EXCLUDED_QNS & _emitted_qns(mock_ingestor))

    def test_exclusion_state_file_is_never_indexed(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """The stamp must not be a file the hash loop watches.

        A state file that indexed itself would be written by every run and so
        differ from its own cached hash on the next one, leaving the fast path
        permanently dead. That is what its CGR_STATE_FILENAMES entry prevents.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        assert (py_project / cs.EXCLUSION_STATE_FILENAME).is_file()
        with (py_project / cs.HASH_CACHE_FILENAME).open() as f:
            assert cs.EXCLUSION_STATE_FILENAME not in json.load(f)

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert updater2._is_already_in_sync() is True

    def test_crash_before_flush_leaves_no_exclusion_stamp(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """A run that dies after Pass 2 must not claim the exclusion is done.

        The module deletions a newly excluded file triggers are only durable
        once the ingestor flushes. Stamping earlier would tell the next run
        the exclusion was already reconciled, and it would fast-path over a
        subtree still in the graph.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()

        stamp = excludable_project / cs.EXCLUSION_STATE_FILENAME
        before = stamp.read_text()

        exclusions = frozenset({"module_a.py"})
        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        )
        processed = False

        def _fail_after_pass_2(*_args: object, **_kwargs: object) -> None:
            if processed:
                raise RuntimeError("ingestor died before the deletions landed")

        real_process_files = updater2._process_files

        def _tracked(*args: object, **kwargs: object) -> None:
            nonlocal processed
            real_process_files(*args, **kwargs)  # type: ignore[arg-type]
            processed = True

        mock_ingestor.flush_all.side_effect = _fail_after_pass_2
        with patch.object(updater2, "_process_files", side_effect=_tracked):
            with pytest.raises(RuntimeError):
                updater2.run()

        mock_ingestor.flush_all.side_effect = None
        assert stamp.read_text() == before

        updater3 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        )
        assert updater3._is_already_in_sync() is False

    def test_crash_before_flush_still_removes_the_excluded_subtree(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """The re-run a lost stamp forces must also DELETE, not just re-parse.

        Declining to stamp only buys the next run a chance to reconcile; it
        cannot take that chance on its own. The hash cache is saved inside
        `_process_files`, before the flush, so the crashed run already
        committed a cache with no `module_a.py` in it, and on the successor
        `deleted_keys` is computed from that cache and comes out empty. The
        graph's own module paths have to join the reconciliation, or the
        subtree survives, the successor stamps a matching exclusion set, and
        every run after that fast-paths over it. The general case, an ordinary
        deletion lost in the same window, is #1615 and is not closed here.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()

        exclusions = frozenset({"module_a.py"})
        mock_ingestor.flush_all.side_effect = RuntimeError("died before the flush")
        with pytest.raises(RuntimeError):
            GraphUpdater(
                ingestor=mock_ingestor,
                repo_path=excludable_project,
                parsers=parsers,
                queries=queries,
                exclude_paths=exclusions,
            ).run()
        mock_ingestor.flush_all.side_effect = None

        # After the crash the graph is the ONLY place `module_a.py` still
        # exists: the hash cache the crashed run committed has already dropped
        # it. So the sink has to be able to answer for it, or the test would
        # pass or fail on the fake's silence rather than on the reconciliation.
        mock_ingestor.reset_mock()
        mock_ingestor.fetch_all.side_effect = lambda query, _params=None: (
            [{cs.KEY_PATH: "module_a.py"}, {cs.KEY_PATH: "module_b.py"}]
            if query == cs.CYPHER_PROJECT_MODULE_PATHS
            else []
        )
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        ).run()
        assert "module_a.py" in _deleted_module_paths(mock_ingestor), (
            "the excluded module survived the crash and the run after it, so "
            "its Module, Class and Method nodes are now permanent: every "
            "later run matches the stamp and never looks again"
        )

    def test_failed_module_query_leaves_no_exclusion_stamp(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """A run whose graph query failed must not claim the exclusion is done.

        When the module-path query raises, the reconciliation falls back to
        the hash cache alone, and after a pre-flush crash that cache no longer
        names the excluded file. The run completes with the subtree still in
        the graph; stamping would make every later run fast-path over it.
        Withholding the stamp is what makes the next run look again.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()
        stamp = excludable_project / cs.EXCLUSION_STATE_FILENAME
        before = stamp.read_text()

        exclusions = frozenset({"module_a.py"})
        mock_ingestor.flush_all.side_effect = RuntimeError("died before the flush")
        with pytest.raises(RuntimeError):
            GraphUpdater(
                ingestor=mock_ingestor,
                repo_path=excludable_project,
                parsers=parsers,
                queries=queries,
                exclude_paths=exclusions,
            ).run()
        mock_ingestor.flush_all.side_effect = None

        def _graph_unreachable(query: str, _params: object = None) -> list[object]:
            if query == cs.CYPHER_PROJECT_MODULE_PATHS:
                raise RuntimeError("graph unreachable")
            return []

        mock_ingestor.reset_mock()
        mock_ingestor.fetch_all.side_effect = _graph_unreachable
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        )
        updater.run()
        assert _module_path_queries(mock_ingestor) == 1
        assert "module_a.py" not in _deleted_module_paths(mock_ingestor), (
            "the fake raised on the module-path query, so nothing could have "
            "named module_a.py for deletion; the fixture is not exercising the "
            "failure it claims to"
        )
        assert stamp.read_text() == before, (
            "the run stamped the new exclusion set although it never learned "
            "what the graph holds, so the surviving subtree is now permanent"
        )

        mock_ingestor.fetch_all.side_effect = lambda query, _params=None: (
            [{cs.KEY_PATH: "module_a.py"}, {cs.KEY_PATH: "module_b.py"}]
            if query == cs.CYPHER_PROJECT_MODULE_PATHS
            else []
        )
        mock_ingestor.reset_mock()
        updater3 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        )
        assert updater3._is_already_in_sync() is False
        updater3.run()
        assert "module_a.py" in _deleted_module_paths(mock_ingestor)
        assert stamp.read_text() != before

    def test_empty_module_query_still_records_the_exclusion_set(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """A query that answers "no modules" is an answer, and the run stamps.

        The withheld stamp above is for a query that RAISED. A readable sink
        that returns no rows has told the run what the graph holds, so the
        reconciliation is complete and the stamp must be written; otherwise
        every run on an empty graph would re-query and warn forever.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()
        stamp = excludable_project / cs.EXCLUSION_STATE_FILENAME
        before = stamp.read_text()

        mock_ingestor.reset_mock()
        mock_ingestor.fetch_all.side_effect = lambda _query, _params=None: []
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=frozenset({"module_a.py"}),
        ).run()
        assert _module_path_queries(mock_ingestor) == 1
        assert stamp.read_text() != before

    def test_memo_is_per_run_not_per_instance(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """One instance run twice must re-read the stamp its own run wrote.

        The memo answers "did the set move since the last COMPLETED run", and
        a run invalidates that answer by stamping at the end. Held across
        `run()` calls it would keep reporting the pre-stamp answer, so the
        instance would never fast-path again and would re-query the graph
        every time. No production caller reuses an instance, which is exactly
        why nothing else would catch this.

        The stamp is removed after the build so the reused instance starts
        from a MISSING one: a full build short-circuits the `or` and leaves
        the memo unset, and an unset memo is recomputed with or without the
        reset, so it could not tell the two apart.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()
        (excludable_project / cs.EXCLUSION_STATE_FILENAME).unlink()

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        )
        mock_ingestor.reset_mock()
        updater.run()
        assert updater._exclusion_match is False, (
            "an index with no stamp carries an unknown set, not a matching one"
        )
        assert updater.skipped_because_in_sync is False
        assert _module_path_queries(mock_ingestor) == 1, (
            "an unknown set must reconcile against the graph exactly once"
        )

        mock_ingestor.reset_mock()
        updater.run()
        assert updater._exclusion_match is True, (
            "the stamp this instance wrote at the end of its own previous run "
            "must be re-read, not answered from the memo it invalidated"
        )
        assert updater.skipped_because_in_sync is True
        assert _module_path_queries(mock_ingestor) == 0, (
            "a matching set costs no graph round-trip"
        )

    def test_memo_reconciles_again_when_the_stamp_changes_underneath(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """The reset has to work in the other direction too.

        Re-reading is only correct if it can turn a match back into a
        mismatch; a memo that refreshed to True once and stuck would pass the
        test above and still be wrong.
        """
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()
        mock_ingestor.reset_mock()
        updater.run()
        assert updater._exclusion_match is True

        (excludable_project / cs.EXCLUSION_STATE_FILENAME).write_text(
            json.dumps({"exclude": ["module_a.py"], "unignore": []})
        )
        mock_ingestor.reset_mock()
        updater.run()
        assert updater._exclusion_match is False, (
            "the stamp on disk no longer matches this run's set, so the "
            "instance must reconcile rather than reuse its own True"
        )
        assert _module_path_queries(mock_ingestor) == 1

    def test_force_bypasses_fast_path(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        updater2 = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with patch.object(updater2, "_process_function_calls") as spy_calls:
            updater2.run(force=True)
            spy_calls.assert_called_once()


class TestSlots:
    def test_function_registry_trie_has_slots(self) -> None:
        assert hasattr(FunctionRegistryTrie, "__slots__")
        trie = FunctionRegistryTrie()
        with pytest.raises(AttributeError):
            trie.nonexistent_attr = "value"  # type: ignore[attr-defined]

    def test_bounded_ast_cache_has_slots(self) -> None:
        assert hasattr(BoundedASTCache, "__slots__")
        cache = BoundedASTCache()
        with pytest.raises(AttributeError):
            cache.nonexistent_attr = "value"  # type: ignore[attr-defined]
