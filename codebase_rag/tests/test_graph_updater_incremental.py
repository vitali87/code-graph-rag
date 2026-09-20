import errno
import json
import os
import time
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import graph_updater as graph_updater_module
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
from codebase_rag.tests.conftest import force_mtime_after_cache


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


def _deleted_module_counts(mock_ingestor: MagicMock) -> Counter[str]:
    return Counter(
        call.args[1][cs.KEY_PATH]
        for call in mock_ingestor.execute_write.call_args_list
        if call.args and call.args[0] == cs.CYPHER_DELETE_MODULE
    )


def _deleted_module_paths(mock_ingestor: MagicMock) -> set[str]:
    return set(_deleted_module_counts(mock_ingestor))


def test_an_edited_file_is_deleted_exactly_once(
    py_project: Path, mock_ingestor: MagicMock
) -> None:
    # The up-front stale-subtree loop is the only delete on the re-parse
    # path; a second per-file delete inside the parse loop is a wasted
    # write per re-indexed file, and a set-typed check cannot see it.
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    ).run()
    module_a = py_project / "module_a.py"
    module_a.write_text(module_a.read_text() + "\n")
    cache_mtime = (py_project / cs.HASH_CACHE_FILENAME).stat().st_mtime
    os.utime(module_a, (cache_mtime + 1, cache_mtime + 1))

    mock_ingestor.reset_mock()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    ).run()

    counts = _deleted_module_counts(mock_ingestor)
    assert "module_a.py" in counts
    assert all(n == 1 for n in counts.values()), dict(counts)


def test_a_deleted_file_is_deleted_exactly_once(
    py_project: Path, mock_ingestor: MagicMock
) -> None:
    # The up-front loop clears a deleted file's subtree before the parse;
    # the post-parse reconciliation must not issue the same delete again.
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    ).run()
    (py_project / "module_a.py").unlink()

    mock_ingestor.reset_mock()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    ).run()

    counts = _deleted_module_counts(mock_ingestor)
    assert counts["module_a.py"] == 1, dict(counts)
    assert all(n == 1 for n in counts.values()), dict(counts)


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
        # The rewrite must land on a later tick than the cache the run just
        # wrote, or the hashing loop skips it; the margin otherwise is only
        # the run's own duration (issue #1640).
        force_mtime_after_cache(py_project, py_project / "module_a.py")

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


class TestCrashBetweenCacheSaveAndFlush:
    """Issue #1615: a run that dies before the graph flush must not convince
    its successor the deletion was reconciled.

    Named for the window as it WAS: the cache used to commit inside
    `_process_files`, so a crash in between left a cache already claiming the
    file was gone. The fix closes the window by committing the cache after the
    flush, so what these tests pin is that no such cache is left behind."""

    def test_edit_during_the_deferred_window_is_not_skipped(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An edit racing the deferred save must still be re-parsed.

        `_is_already_in_sync` skips rehashing any file whose mtime is at or
        below the CACHE FILE's own mtime. Deferring the write to the post-flush
        commit point moved that stamp later, so a file edited after
        `_process_files` hashed it but before the save would be stamped newer
        than its own edit and skipped for good. The recorded mtime has to be
        the one observed at hash time, not one implied by when the write
        happened.
        """
        parsers, queries = load_parsers()
        target = py_project / "module_b.py"
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        # Edit in the true window: after `_process_files` hashed the file,
        # before the deferred write stamps the cache. Wrapping the save is the
        # only way to land there; `flush_all` fires BEFORE the commit point, so
        # an edit there precedes the cache write and cannot reproduce this.
        # Give run 2 real work, or it takes the in-sync fast path and returns
        # above the commit point, so the save never fires and the wrapper below
        # never lands its edit.
        (py_project / "module_c.py").write_text(
            "def c():\n    return 1\n", encoding="utf-8"
        )

        edited = "def b():\n    return 999\n"
        real_save = graph_updater_module._publish_hash_cache

        reached_write_site = False

        def _edit_then_save(
            path: Path, hashes: dict[str, str], observed_at: float | None
        ) -> bool:
            nonlocal reached_write_site
            reached_write_site = True
            # The observation instant is already captured by now, and this
            # edit must be measurably later than it rather than landing on the
            # same filesystem tick; 200 ms clears every CI platform's mtime
            # granularity, the coarsest being Windows at 15.6 ms.
            # The pause moves nothing, it only waits, which is why it cannot
            # pin the assertion below: break the fix and the cache is stamped
            # after this point regardless of how long the edit waited, so it
            # is still the newer of the two and the successor still skips.
            time.sleep(0.2)
            target.write_text(edited, encoding="utf-8")
            # Pass the real verdict through: swallowing it would report a
            # failed publish as a success and let the directory-mtime map
            # advance past a cache that never landed.
            return bool(real_save(path, hashes, observed_at))

        monkeypatch.setattr(
            graph_updater_module, "_publish_hash_cache", _edit_then_save
        )
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()
        monkeypatch.setattr(graph_updater_module, "_publish_hash_cache", real_save)

        # Three guards, each catching a different lie. The first two are what
        # caught two wrong versions of this test: an edit placed in `flush_all`
        # (which fires BEFORE the commit point, so it could not reach the
        # window at all), and a second run that took the in-sync fast path and
        # returned above the write site, so the wrapper never fired.
        assert reached_write_site, (
            "the run never reached the deferred write, so it cannot have "
            "raced anything; the assertion below would pass on an unfixed "
            "tree for a reason unrelated to the window"
        )
        assert target.read_text(encoding="utf-8") == edited, (
            "fixture guard: the racing edit did not land, so nothing below "
            "measures the window it claims to"
        )
        successor = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert successor._is_already_in_sync() is False, (
            "the edit made during the deferred window is invisible: its mtime "
            "is at or below the cache file's own stamp, so the successor skips "
            "rehashing it and the graph keeps the pre-edit contents"
        )

    def test_edit_between_a_files_hash_and_the_end_of_the_loop_is_not_skipped(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An edit landing after a file's own hash but before the loop ends.

        The observation instant is captured before the hashing loop rather
        than after it, so it is at or earlier than every hash it describes.
        Captured after the loop it would be an upper bound, and this edit
        would be stamped newer than its own change and skipped for good.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        # Real work for run 2, so it cannot take the in-sync fast path and
        # return above the hashing loop this test needs it to enter.
        (py_project / "module_c.py").write_text(
            "def c():\n    return 1\n", encoding="utf-8"
        )
        target = py_project / "module_b.py"
        target.write_text("def b():\n    return 2\n", encoding="utf-8")

        real_hash = graph_updater_module._hash_file_with_bytes
        raced = "def b():\n    return 999\n"
        fired = False

        def _hash_then_edit(path: Path, *args: object, **kwargs: object) -> object:
            nonlocal fired
            result = real_hash(path, *args, **kwargs)
            if path.name == "module_b.py" and not fired:
                fired = True
                # After this file's own hash is taken, still inside the loop.
                # As in the test above, the pause makes this edit measurably
                # later than the captured instant instead of trusting the
                # filesystem to separate two writes made in the same moment.
                # It moves nothing, it only waits: it widens a gap the fix
                # creates and the unfixed placement reverses, so it cannot
                # decide the assertion either way.
                time.sleep(0.2)
                path.write_text(raced, encoding="utf-8")
            return result

        monkeypatch.setattr(
            graph_updater_module, "_hash_file_with_bytes", _hash_then_edit
        )
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()
        monkeypatch.setattr(graph_updater_module, "_hash_file_with_bytes", real_hash)

        assert fired, (
            "the wrapper never fired, so no edit raced the loop and the "
            "assertion below would pass for a reason unrelated to the window"
        )
        assert target.read_text(encoding="utf-8") == raced, (
            "fixture guard: the racing edit did not land"
        )
        successor = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert successor._is_already_in_sync() is False, (
            "an edit made after its own file's hash but before the loop ended "
            "is invisible: its mtime is at or below the cache stamp, so the "
            "successor skips rehashing it and the edit is lost for good"
        )

    def test_a_read_only_tree_still_completes_the_run(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A filesystem that refuses every publish step must not end the run.

        Replaces the two tests that pinned the previous fail-closed handler
        (a live cache stamped in place, removed when the stamp failed). That
        code path is gone: the cache is built on a temporary path and renamed,
        so the live file is never mutated and never needs removing. The OUTER
        contract those tests existed for survives and is what this asserts:
        indexing work must not be forfeited because a cache could not be
        written. All three publish steps are refused, but the write is the one
        that fires - it short-circuits `_publish_hash_cache` before the stamp
        or the rename is reached - so the `utime` and `replace` refusals stand
        as guards against a future reordering rather than as live assertions.
        The cleanup branch they leave unexercised is covered by the test
        below.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        cache = py_project / cs.HASH_CACHE_FILENAME
        before = cache.read_text(encoding="utf-8")
        assert "module_b.py" in before, (
            "fixture guard: run 1 wrote no usable cache, so leaving it intact "
            "below would prove nothing"
        )

        (py_project / "module_c.py").write_text(
            "def c():\n    return 1\n", encoding="utf-8"
        )

        real_open = Path.open
        real_os_open = os.open
        real_utime = os.utime
        real_replace = os.replace
        refused: set[str] = set()

        def _is_cache_temp(path: Path) -> bool:
            return path.name.startswith(cs.HASH_CACHE_FILENAME) and path.name.endswith(
                ".tmp"
            )

        def _refuse_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if _is_cache_temp(self):
                refused.add("write")
                raise OSError(errno.EROFS, "Read-only file system")
            return real_open(self, *args, **kwargs)

        # The publish creates its temporary through `os.open` with
        # O_EXCL|O_NOFOLLOW rather than `Path.open` (issue #1700), so the
        # read-only filesystem has to be simulated at that call too. Patching
        # only `Path.open` left the write unrefused, which the `"write" in
        # refused` assertion below catches rather than passing vacuously.
        def _refuse_os_open(path, flags, mode=0o777, **kwargs):  # type: ignore[no-untyped-def]
            if _is_cache_temp(Path(os.fspath(path))):
                refused.add("write")
                raise OSError(errno.EROFS, "Read-only file system")
            return real_os_open(path, flags, mode, **kwargs)

        def _refuse_utime(path, times=None, **kwargs):  # type: ignore[no-untyped-def]
            if _is_cache_temp(Path(path)):
                refused.add("utime")
                raise OSError(errno.EROFS, "Read-only file system")
            return real_utime(path, times, **kwargs)

        def _refuse_replace(src, dst, **kwargs):  # type: ignore[no-untyped-def]
            if Path(dst).name == cs.HASH_CACHE_FILENAME:
                refused.add("replace")
                raise OSError(errno.EROFS, "Read-only file system")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(Path, "open", _refuse_open)
        monkeypatch.setattr(graph_updater_module.os, "open", _refuse_os_open)
        monkeypatch.setattr(graph_updater_module.os, "utime", _refuse_utime)
        monkeypatch.setattr(graph_updater_module.os, "replace", _refuse_replace)
        # Must COMPLETE: a cache that cannot be written is not a failed run.
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()
        monkeypatch.setattr(graph_updater_module.os, "replace", real_replace)
        monkeypatch.setattr(graph_updater_module.os, "utime", real_utime)
        monkeypatch.setattr(graph_updater_module.os, "open", real_os_open)
        monkeypatch.setattr(Path, "open", real_open)

        assert "write" in refused, (
            "the run never attempted the temporary cache write, so completing "
            f"says nothing about a read-only tree; refused={sorted(refused)}"
        )
        assert cache.read_text(encoding="utf-8") == before, (
            "the refused publish damaged the previous cache; it must be left "
            "exactly as the last successful run wrote it"
        )
        leftovers = sorted(q.name for q in py_project.glob("*.tmp"))
        assert not leftovers, f"temporary cache files were left behind: {leftovers}"

    def test_a_failed_publish_does_not_advance_the_directory_mtimes(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A hash cache that did not publish must not advance the map.

        The two artefacts are only safe as a PAIR. A failed publish leaves the
        previous hash cache on disk, so advancing the directory-mtime map
        builds the fresh-map/stale-cache combination the publish order exists
        to avoid: `_is_already_in_sync` compares every recorded directory
        against a map that already calls it current, finds nothing changed,
        and the file loop walks only the keys the OLD cache names. A file
        added during the failed run is then never indexed at all.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        mtimes_path = py_project / cs.DIR_MTIMES_FILENAME
        before = mtimes_path.read_text(encoding="utf-8")
        assert before.strip() not in ("", "{}"), (
            "fixture guard: run 1 recorded no directory mtimes, so an "
            "unchanged map below would prove nothing"
        )

        # An addition is what the pair has to keep visible, and it also moves
        # the containing directory's mtime so run 2 has something new to
        # record. Without it the map could be byte-identical for the trivial
        # reason that nothing changed.
        (py_project / "module_c.py").write_text(
            "def c():\n    return 1\n", encoding="utf-8"
        )

        real_replace = os.replace
        reached = False

        def _refuse_replace(src: object, dst: object) -> None:
            nonlocal reached
            if str(src).endswith(".tmp"):
                reached = True
                raise OSError(errno.EROFS, "Read-only file system")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", _refuse_replace)

        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        monkeypatch.undo()

        assert reached, (
            "the run never attempted the atomic rename, so the assertion "
            "below would hold for a run that simply had nothing to publish"
        )
        assert mtimes_path.read_text(encoding="utf-8") == before, (
            "the directory-mtime map advanced past a hash cache that failed "
            "to publish, leaving the pair that hides an addition"
        )

        # The pair is still consistent, which is the point of withholding it:
        # the next run must NOT take the fast path, so module_c is indexed.
        assert not GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )._is_already_in_sync(), (
            "the successor took the in-sync fast path, so the file added "
            "during the failed run is never indexed"
        )

    def test_a_temp_that_cannot_be_cleaned_up_does_not_end_the_run(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The publish fails after the temp exists, and the temp cannot go.

        Restores the pairing the #1644 tests pinned before the publish became
        atomic: one failure the handler expects, plus a second failure in the
        handler's own cleanup. The test above cannot reach this, because its
        write refusal short-circuits before any temp file exists. A leftover
        temp is inert, since nothing reads a `.tmp` path, so the run must
        finish and the previous cache must survive untouched.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        cache = py_project / cs.HASH_CACHE_FILENAME
        before = cache.read_text(encoding="utf-8")
        assert "module_b.py" in before, (
            "fixture guard: run 1 wrote no usable cache to preserve"
        )

        (py_project / "module_c.py").write_text(
            "def c():\n    return 1\n", encoding="utf-8"
        )

        real_replace = os.replace
        real_unlink = Path.unlink
        reached: set[str] = set()

        def _refuse_replace(src, dst, **kwargs):  # type: ignore[no-untyped-def]
            if Path(dst).name == cs.HASH_CACHE_FILENAME:
                reached.add("replace")
                raise OSError(errno.EROFS, "Read-only file system")
            return real_replace(src, dst, **kwargs)

        def _refuse_unlink(self, missing_ok=False):  # type: ignore[no-untyped-def]
            if self.name.endswith(".tmp"):
                reached.add("cleanup")
                raise OSError(errno.EACCES, "Permission denied")
            return real_unlink(self, missing_ok=missing_ok)

        messages: list[str] = []
        sink_id = graph_updater_module.logger.add(messages.append, level="WARNING")
        monkeypatch.setattr(graph_updater_module.os, "replace", _refuse_replace)
        monkeypatch.setattr(Path, "unlink", _refuse_unlink)
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()
        monkeypatch.setattr(Path, "unlink", real_unlink)
        monkeypatch.setattr(graph_updater_module.os, "replace", real_replace)
        # Removed here rather than left to the session: a sink that outlives
        # this test appends to `messages` for every later test that warns.
        graph_updater_module.logger.remove(sink_id)

        cleanup_warnings = [m for m in messages if "temporary cache file" in m]
        assert cleanup_warnings, (
            "the cleanup failure was never logged, so its reported reason "
            "cannot be checked"
        )
        # The publish failed EROFS and the removal EACCES; reporting the
        # publish reason here would name a cause unrelated to the removal.
        assert "Permission denied" in cleanup_warnings[0], (
            "the cleanup warning reports the publish failure rather than why "
            f"the removal failed: {cleanup_warnings[0]}"
        )
        assert reached == {"replace", "cleanup"}, (
            "the run did not reach both the failed rename and the failed "
            f"cleanup, so finishing proves nothing about the pairing; {reached}"
        )
        assert cache.read_text(encoding="utf-8") == before, (
            "the failed publish damaged the previous cache"
        )

    def test_a_stop_between_the_two_cache_publishes_is_still_detected(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two caches are published in the only safe order.

        They are separate files, so a run stopping between them leaves a
        mismatched pair, and the pair is trusted in only ONE direction. The
        sync check iterates the entries the map RECORDS and so never reaches a
        directory the map omits; what catches an addition is the recorded
        entry for the directory that DIRECTLY CONTAINS it, whose stored mtime
        no longer matches disk. For a new subdirectory that is the parent; for
        a file added to a directory already in the map it is that directory's
        own entry. A FRESH map records them all as current, nothing is
        compared, and the file loop only walks keys the hash cache already
        names, so a file added in the gap is never indexed. Publishing the
        hash cache first leaves the harmless window, where a stale map still
        holds the old mtimes and the addition surfaces.

        The stop is keyed on whichever publish happens SECOND rather than on
        either by name, so reversing the two calls still constructs a real
        mismatched pair and this measures the ordering rather than the fixture.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        package = py_project / "pkg"
        package.mkdir()
        (package / "added.py").write_text(
            "def added():\n    return 1\n", encoding="utf-8"
        )

        real_publish = graph_updater_module._publish_hash_cache
        real_dir_mtimes = graph_updater_module._save_dir_mtimes
        order: list[str] = []

        def _publish_unless_second(
            path: Path, hashes: dict[str, str], observed_at: float | None
        ) -> bool:
            order.append("hash_cache")
            if len(order) == 1:
                return bool(real_publish(path, hashes, observed_at))
            # The STOP this test models is the process dying between the two
            # publishes, not a publish that failed. Reporting success is what
            # keeps the caller reaching `_save_dir_mtimes`, which is the call
            # whose ordering is under test.
            return True

        def _dir_mtimes_unless_second(path: Path, mtimes: dict[str, float]) -> None:
            order.append("dir_mtimes")
            if len(order) == 1:
                real_dir_mtimes(path, mtimes)

        monkeypatch.setattr(
            graph_updater_module, "_publish_hash_cache", _publish_unless_second
        )
        monkeypatch.setattr(
            graph_updater_module, "_save_dir_mtimes", _dir_mtimes_unless_second
        )
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()
        monkeypatch.setattr(graph_updater_module, "_publish_hash_cache", real_publish)
        monkeypatch.setattr(graph_updater_module, "_save_dir_mtimes", real_dir_mtimes)

        assert order == ["hash_cache", "dir_mtimes"], (
            "the commit point did not publish the hash cache first, so the "
            f"surviving window is the trusted one; saw {order}"
        )
        successor = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert successor._is_already_in_sync() is False, (
            "the successor trusts the mismatched pair this stop left, so the "
            "file added in the gap is never indexed"
        )

    def test_successor_run_still_deletes_the_module(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        (py_project / "module_b.py").unlink()

        # Run 2 dies at the commit point: the deletes are queued but never
        # flushed. Whatever this run wrote to disk is all its successor has.
        crashed = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        with (
            patch.object(
                mock_ingestor, "flush_all", side_effect=RuntimeError("database gone")
            ),
            pytest.raises(RuntimeError),
        ):
            crashed.run()

        # Run 3 is the one under test: it must re-issue the delete the crashed
        # run only queued. Asserting on the delete itself, not on the sync
        # check, because a stale-cache no-op also leaves the fast path off.
        mock_ingestor.reset_mock()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        assert "module_b.py" in _deleted_module_paths(mock_ingestor)

    def test_prune_deletes_are_flushed_before_the_cache_commits(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Issue #1645: the same defect on the orphan-prune writes.

        `_prune_orphan_nodes` runs after the graph flush and issues its own
        `execute_write` deletes. Without a flush of its own, those deletes are
        still queued when the hash cache commits, so a run that stops in
        between hands its successor caches describing a tree whose prune was
        never applied. The successor takes the in-sync fast path and never
        re-issues them, and the orphan survives until a full rebuild.
        """
        parsers, queries = load_parsers()

        # A Folder row for a directory that is not on disk is an orphan, so
        # the prune must delete it. Driving a REAL delete is what stops this
        # test being vacuous: the default `fetch_all` mock iterates empty, the
        # prune finds nothing, and every ordering assertion below would hold
        # against unfixed code simply because no delete was ever issued.
        ghost = (py_project / "gone").resolve().as_posix()

        def _fetch_all(query: str) -> list[dict[str, str]]:
            if query == cs.CYPHER_ALL_FOLDER_PATHS:
                return [{cs.KEY_PATH: "gone", "absolute_path": ghost}]
            return []

        mock_ingestor.fetch_all = MagicMock(side_effect=_fetch_all)

        order: list[str] = []

        def _record_write(*args: object, **kwargs: object) -> MagicMock:
            if args and args[0] == cs.CYPHER_DELETE_FOLDER:
                order.append("prune_delete")
            return MagicMock()

        def _record_flush(*args: object, **kwargs: object) -> MagicMock:
            order.append("flush")
            return MagicMock()

        mock_ingestor.execute_write = MagicMock(side_effect=_record_write)
        mock_ingestor.flush_all = MagicMock(side_effect=_record_flush)

        real_publish = graph_updater_module._publish_hash_cache

        def _record_publish(*args: object, **kwargs: object) -> None:
            order.append("cache_commit")
            real_publish(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(graph_updater_module, "_publish_hash_cache", _record_publish):
            GraphUpdater(
                ingestor=mock_ingestor,
                repo_path=py_project,
                parsers=parsers,
                queries=queries,
            ).run()

        # The fixture must actually exercise the prune, or the ordering
        # assertion below is satisfied by a run that pruned nothing.
        assert "prune_delete" in order, f"prune issued no delete: {order}"
        assert "cache_commit" in order, f"cache never committed: {order}"

        # The property: every prune delete is durable before the cache that
        # claims the tree was reconciled becomes visible. Asserting on a flush
        # BETWEEN the two, not merely that a flush exists: the run already
        # flushes before the prune, so `flush_all.called` is true either way.
        deleted_at = order.index("prune_delete")
        committed_at = order.index("cache_commit")
        assert "flush" in order[deleted_at:committed_at], (
            f"prune deletes were not flushed before the hash cache committed: {order}"
        )


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
        # Same precondition as above: the fast path compares this mtime with
        # the cache's, and equal ticks read as unchanged (issue #1640).
        force_mtime_after_cache(py_project, py_project / "module_a.py")

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

    def test_a_cache_stamped_in_the_future_does_not_skip_an_edit(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """Issue #1665: a future mtime makes every file look unchanged.

        `_is_already_in_sync` and the hashing loop both fast-path a file whose
        mtime is at or below the cache's. A cache stamped ahead of the wall
        clock satisfies that for EVERY file on disk, however recently edited,
        so nothing is re-indexed and the graph silently stops tracking the
        tree. It does not self-correct: the stamp stays ahead, so every later
        run skips too.

        The causes are ordinary rather than exotic -- an NTP correction
        backwards, VM suspend/resume, `cp -p` or `rsync -t` from a host whose
        clock ran ahead, a restored backup or copied image.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        cache = py_project / cs.HASH_CACHE_FILENAME
        assert cache.is_file(), "fixture guard: run 1 wrote no cache to stamp"
        ahead = time.time() + 3600
        os.utime(cache, (ahead, ahead))

        # Edited AFTER the stamp, so its mtime is older than the cache's while
        # being newer than the content the cache describes -- the exact shape
        # the comparison cannot distinguish.
        (py_project / "module_a.py").write_text(
            "def edited_after_the_stamp():\n    return 2\n", encoding="utf-8"
        )

        successor = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert successor._is_already_in_sync() is False, (
            "a cache stamped in the future reported the tree as in sync, so "
            "the edit is never indexed and no later run corrects it"
        )

        # And the edit must actually REACH the graph. The assertion above is
        # satisfied by any reason for declining the fast path, including a
        # rescan that finds nothing wrong: it pins "did not take the shortcut",
        # not "did not lose the edit", and those are different claims. Skipping
        # silently is the dangerous direction here; a redundant re-hash is the
        # safe error, so the outcome is what has to be asserted. Measured
        # against the unfixed helper, where the new symbol never appears.
        mock_ingestor.reset_mock()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()
        indexed_functions = {
            str(call.args[1].get(cs.KEY_QUALIFIED_NAME))
            for call in mock_ingestor.ensure_node_batch.call_args_list
            if call.args[0] == cs.NodeLabel.FUNCTION
        }
        assert any("edited_after_the_stamp" in qn for qn in indexed_functions), (
            "the edit never reached the graph against a future stamp: "
            f"{sorted(indexed_functions)}"
        )

    def test_a_stamp_that_cannot_be_read_is_not_trusted(self, tmp_path: Path) -> None:
        """The third answer `_trustworthy_cache_mtime` gives: unreadable.

        Both readers only reach it once `cache_path.is_file()` or the fast
        path's own checks said the cache exists, so a failing `stat` here is a
        race with a concurrent publish or a permissions change underneath the
        run. The honest watermark for a stamp that cannot be read is the same
        as for one that cannot be trusted: 0.0, so nothing is skipped on its
        account and the hash comparison decides.

        A file in the path where a directory should be makes `stat` raise
        `NotADirectoryError`, an `OSError`, without touching permissions,
        which behave differently for root and on Windows.
        """
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_text("x", encoding="utf-8")
        unreadable = not_a_dir / "cache.json"
        with pytest.raises(OSError):
            unreadable.stat()  # fixture guard: the stat must actually fail

        assert graph_updater_module._trustworthy_cache_mtime(unreadable) == 0.0

        # The control: a readable, sane stamp is returned as it is, so the
        # 0.0 above is the OSError branch and not a helper that always
        # answers 0.0.
        readable = tmp_path / "cache.json"
        readable.write_text("{}", encoding="utf-8")
        os.utime(readable, (1_000_000.0, 1_000_000.0))
        assert graph_updater_module._trustworthy_cache_mtime(readable) == 1_000_000.0

    def test_a_future_stamp_does_not_skip_an_edit_in_the_hashing_loop(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """The SECOND reader of the stamp, at the loop that actually indexes.

        `_is_already_in_sync` is only the fast path. When it declines, the run
        reaches the hashing loop, which applies the same `mtime <=
        cache_mtime` shortcut against the same stamp -- and that shortcut
        `continue`s BEFORE the hash comparison, so the hash is not the
        authority here the way it is for the sync check. The stale hash is
        then copied into the new cache and the stamp rewritten sanely, so the
        lie becomes indistinguishable from truth and no later run corrects it.

        The unrelated addition is what makes this test about the LOOP: it
        makes the sync check decline for a reason other than the edit itself,
        which is the only way the poisoned stamp reaches the loop at all.
        Without it the sync check answers first and this re-tests the reader
        above.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        cache = py_project / cs.HASH_CACHE_FILENAME
        assert cache.is_file(), "fixture guard: run 1 wrote no cache to stamp"
        ahead = time.time() + 3600
        os.utime(cache, (ahead, ahead))

        (py_project / "module_a.py").write_text(
            "def edited_under_a_future_stamp():\n    return 2\n", encoding="utf-8"
        )
        (py_project / "module_new.py").write_text(
            "def unrelated():\n    return 3\n", encoding="utf-8"
        )

        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        # The cache must carry the file's REAL hash. Asserting on the graph
        # would not do: the edited file is re-parsed either way once the run
        # proceeds, and it is the persisted stale hash that loses the edit for
        # every later run.
        cached = json.loads(cache.read_text(encoding="utf-8")).get("module_a.py")
        assert cached == _hash_file(py_project / "module_a.py"), (
            "the hashing loop skipped the edited file against a future stamp "
            "and persisted its stale hash, so no later run sees the edit"
        )

    def test_a_single_file_run_over_a_distrusted_cache_keeps_sibling_edits(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """The stamp WRITER, which the two readers above do not reach.

        A single-file run carries the previous cache mtime forward so the
        siblings it never looked at keep their meaning. When that previous
        stamp was distrusted for being in the future it reads as 0.0, and
        `cache_mtime or None` -- falsy 0.0 -- takes the `None` branch the
        comment beside it explicitly warns against: `None` skips the
        `os.utime`, the cache keeps its own write time (NOW), and a sibling
        edited before the run is then older than the stamp, so every later
        project run reports "already in sync" and the edit is lost for good.

        So the read guard alone made the write worse. The epoch vouches for
        nothing, which is the honest claim after an untrustworthy stamp;
        `None` vouches for now.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        ).run()

        cache = py_project / cs.HASH_CACHE_FILENAME
        assert cache.is_file(), "fixture guard: run 1 wrote no cache to stamp"
        ahead = time.time() + 3600
        os.utime(cache, (ahead, ahead))

        # Edited BEFORE the single-file run, and never looked at by it: this
        # is the sibling whose edit the stamp has to keep visible.
        (py_project / "module_b.py").write_text(
            "def b_edited_before_the_single_file_run():\n    return 2\n",
            encoding="utf-8",
        )

        single = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        single._single_file = py_project / "module_a.py"
        single.run()

        successor = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        assert successor._is_already_in_sync() is False, (
            "the single-file run stamped the cache with its own write time, "
            "so the sibling edited before it is never indexed again"
        )

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
        cannot take that chance on its own. The cache reaches the state that
        matters through a COMPLETED excluding run, which drops `module_a.py`
        from it; after that no on-disk hash names the file and the graph's own
        module paths are the only thing that can, so they have to join the
        reconciliation or the subtree survives, the successor stamps a matching
        exclusion set, and every run after that fast-paths over it.

        Reached this way rather than through a pre-flush crash: since #1615 a
        crashed run commits no cache at all, so it leaves the PREVIOUS run's
        cache intact and that one still names `module_a.py`. A crash-based
        fixture would let `old_hashes` supply the deletion on its own and the
        test would pass with the graph query neutered, which is what it is
        here to rule out. The control at the end pins exactly that.
        """
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
        ).run()

        exclusions = frozenset({"module_a.py"})
        # A COMPLETED excluding run is what drops module_a.py from the cache.
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        ).run()
        cache = json.loads((excludable_project / cs.HASH_CACHE_FILENAME).read_text())
        assert "module_a.py" not in cache, (
            "fixture guard: the cache still names module_a.py, so old_hashes "
            "could supply the deletion and the graph query would not be "
            "measured by anything below"
        )
        # The stamp is what would let the next run fast-path over the subtree.
        (excludable_project / cs.EXCLUSION_STATE_FILENAME).unlink()

        # The graph is now the ONLY place module_a.py still exists, so the sink
        # has to be able to answer for it or the test would turn on the fake's
        # silence rather than on the reconciliation.
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
            "the excluded module survived the run after it, so its Module, "
            "Class and Method nodes are now permanent: every later run "
            "matches the stamp and never looks again"
        )

        # Control: with the graph unable to name it, the delete must VANISH.
        # Without this the assertion above passes whether or not the graph
        # query contributes anything, which is what happened when #1615
        # changed where the cache commits and left this test measuring
        # `old_hashes` instead.
        (excludable_project / cs.EXCLUSION_STATE_FILENAME).unlink()
        mock_ingestor.reset_mock()
        with patch.object(
            GraphUpdater, "_existing_module_paths", return_value=frozenset()
        ):
            GraphUpdater(
                ingestor=mock_ingestor,
                repo_path=excludable_project,
                parsers=parsers,
                queries=queries,
                exclude_paths=exclusions,
            ).run()
        assert "module_a.py" not in _deleted_module_paths(mock_ingestor), (
            "the delete fired with the graph contributing nothing, so this "
            "test no longer measures the graph-backed reconciliation it "
            "claims to and would pass with that reconciliation removed"
        )

    def test_failed_module_query_leaves_no_exclusion_stamp(
        self, excludable_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """A run whose graph query failed must not claim the exclusion is done.

        When the module-path query raises, the reconciliation falls back to
        the hash cache alone, and that cache no longer names a file excluded
        by an EARLIER completed run. The run completes with the subtree still
        in the graph; stamping would make every later run fast-path over it.
        Withholding the stamp is what makes the next run look again.

        The stale cache is reached here through a completed run rather than a
        pre-flush crash: since #1615 the cache commits only after the flush,
        so a crashed run leaves no cache to be stale. Removing the stamp
        afterwards is the "no readable record" case, an index predating the
        stamp or a corrupt file, which is what puts run 3 back into
        reconciliation with a cache that has already forgotten module_a.py.
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
        GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=excludable_project,
            parsers=parsers,
            queries=queries,
            exclude_paths=exclusions,
        ).run()
        # The committed cache no longer names module_a.py, so the cache-based
        # deletion set is empty from here on; only the graph could name it.
        stamp.unlink()

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
        assert not stamp.exists(), (
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
        # The recovered run learned what the graph holds, so it records the
        # exclusion set again: a stamp exists once more, and for this set.
        assert stamp.exists()
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
        mock_ingestor.fetch_all.side_effect = None
        mock_ingestor.fetch_all.return_value = []
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

    def test_skipped_flag_is_per_run_not_per_instance(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        """A reused instance must not carry a previous run's skip into this one.

        `skipped_because_in_sync` is set on the in-sync early return and was
        only ever cleared in `__init__`, so once an instance skipped it kept
        reporting True for every later `run()` however much work that run did.
        `cli.py` reads the flag to decide what to tell the user, so a stale
        True reports "already in sync" for a run that re-parsed the repo.

        The order matters: the flag must be observed True first, or a run that
        never set it would satisfy the second assertion on its own and the
        test would pass against the unfixed code (#1620).
        """
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=py_project,
            parsers=parsers,
            queries=queries,
        )
        updater.run()
        updater.run()
        assert updater.skipped_because_in_sync is True, (
            "second run on an unchanged repo must take the in-sync fast path, "
            "otherwise the reset below is never exercised"
        )

        updater.run(force=True)
        assert updater.skipped_because_in_sync is False, (
            "a forced run does real work, so the flag must reflect THIS run "
            "rather than the skip recorded by the previous one"
        )


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


def test_a_graph_only_module_is_deleted_before_the_first_parse(
    py_project: Path, mock_ingestor: MagicMock
) -> None:
    # Cacheless rebuild over an existing graph: module_c.py is gone from
    # disk and was never in a cache, only the graph names it. Its subtree
    # must go before any file is parsed, or a same-stem survivor parsed
    # first claims its qn and the later delete by path finds nothing.
    parsers, queries = load_parsers()

    def module_paths(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_PROJECT_MODULE_PATHS:
            return [
                {cs.KEY_PATH: "module_a.py"},
                {cs.KEY_PATH: "module_b.py"},
                {cs.KEY_PATH: "module_c.py"},
            ]
        return []

    mock_ingestor.fetch_all.side_effect = module_paths
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    )
    deleted_at_first_parse: list[set[str]] = []
    real_parse = updater._process_single_file

    def spy(*args: object, **kwargs: object) -> None:
        if not deleted_at_first_parse:
            deleted_at_first_parse.append(_deleted_module_paths(mock_ingestor))
        real_parse(*args, **kwargs)

    updater._process_single_file = spy  # type: ignore[method-assign]
    updater.run()

    assert deleted_at_first_parse, "no file was parsed"
    assert "module_c.py" in deleted_at_first_parse[0]
    assert _deleted_module_counts(mock_ingestor)["module_c.py"] == 1


def test_a_parse_failure_still_rebuilds_the_other_changed_modules(
    py_project: Path, mock_ingestor: MagicMock
) -> None:
    # Every changed module's old subtree is deleted before the first parse,
    # so a file that fails to parse must not abort the loop: the modules
    # after it would stay deleted and never be rebuilt. The error still
    # surfaces once the remaining files are processed.
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    ).run()
    cache_mtime = (py_project / cs.HASH_CACHE_FILENAME).stat().st_mtime
    for name in ("module_a.py", "module_b.py"):
        path = py_project / name
        path.write_text(path.read_text() + "\n")
        os.utime(path, (cache_mtime + 1, cache_mtime + 1))

    mock_ingestor.reset_mock()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    )
    real_parse = updater._process_single_file
    parsed: list[str] = []

    def flaky(filepath: Path, *args: object, **kwargs: object) -> None:
        if filepath.name == "module_a.py":
            raise RuntimeError("parse died")
        parsed.append(filepath.name)
        real_parse(filepath, *args, **kwargs)

    updater._process_single_file = flaky  # type: ignore[method-assign]
    errors: list[str] = []
    sink_id = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        with pytest.raises(RuntimeError, match="parse died"):
            updater.run()
    finally:
        logger.remove(sink_id)

    # Both subtrees were deleted up front; only the survivor was rebuilt.
    assert {"module_a.py", "module_b.py"} <= _deleted_module_paths(mock_ingestor)
    assert "module_b.py" in parsed
    # The re-raised error carries the message, not the file: only the log
    # names which file failed.
    assert any("module_a.py" in line and "parse died" in line for line in errors)


def test_a_pre_parse_failure_leaves_the_old_subtrees_in_place(
    py_project: Path, mock_ingestor: MagicMock
) -> None:
    # The stale-subtree deletes run only once every changed file has
    # pre-parsed; a failure there must not leave an emptied graph behind.
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    ).run()
    module_a = py_project / "module_a.py"
    module_a.write_text(module_a.read_text() + "\n")
    cache_mtime = (py_project / cs.HASH_CACHE_FILENAME).stat().st_mtime
    os.utime(module_a, (cache_mtime + 1, cache_mtime + 1))

    mock_ingestor.reset_mock()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=py_project, parsers=parsers, queries=queries
    )
    with (
        patch.object(
            updater, "_pre_parse_changed_files", side_effect=RuntimeError("parse died")
        ),
        pytest.raises(RuntimeError, match="parse died"),
    ):
        updater.run()

    assert _deleted_module_paths(mock_ingestor) == set()


class TestHashCachePublishSymlinkSafety:
    """The publish must not write through a link planted at its temp path."""

    def test_a_planted_symlink_at_the_predictable_path_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        """A pre-placed link at the OLD predictable name must be inert.

        The temporary name used to be `<cache>.<pid>.tmp` and was opened with
        `Path.open("w")`, which follows a symlink: a local process able to
        write to the repository directory could point that name at any file
        the publisher could write and have it truncated and replaced with
        cache JSON (issue #1700).

        The victim is asserted BY CONTENT rather than by mtime or size: the
        overwrite leaves a perfectly well-formed file, so only the bytes
        distinguish "untouched" from "replaced with someone else's data".
        """
        cache_path = tmp_path / "cache.json"
        victim = tmp_path / "victim.txt"
        original = "precious contents that must survive\n"
        victim.write_text(original, encoding="utf-8")

        planted = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
        planted.symlink_to(victim)

        published = graph_updater_module._publish_hash_cache(
            cache_path, {"a.py": "deadbeef"}, None
        )

        assert victim.read_text(encoding="utf-8") == original, (
            "the publish followed a symlink planted at its temporary path and "
            "overwrote the link's target with cache JSON"
        )
        assert published, "the publish must still succeed around a planted link"
        assert json.loads(cache_path.read_text(encoding="utf-8")) == {
            "a.py": "deadbeef"
        }, "the cache itself must be written correctly"

    def test_the_temporary_name_is_not_derived_from_the_pid(
        self, tmp_path: Path
    ) -> None:
        """Two publishes must not reuse one predictable temporary name.

        Pins the unpredictability directly. Asserting only that the planted
        link survives would still pass if the name were merely CHANGED to
        another fixed string, which an attacker reads out of the source just
        as easily.
        """
        seen: set[str] = set()
        real_open = os.open

        def _record(path, flags, mode=0o777, **kwargs):  # type: ignore[no-untyped-def]
            name = os.fspath(path)
            if name.endswith(".tmp"):
                seen.add(os.path.basename(name))
            return real_open(path, flags, mode, **kwargs)

        # The SAME cache path both times. Publishing to cache0/cache1 would
        # yield two distinct temporary names from any scheme at all, including
        # a fixed `.tmp` suffix, so it could not tell an unpredictable name
        # from a derived one (#1701 review).
        cache_path = tmp_path / "cache.json"
        with patch.object(os, "open", _record):
            for _ in range(2):
                graph_updater_module._publish_hash_cache(
                    cache_path, {"a.py": "x"}, None
                )

        assert len(seen) == 2, (
            "two publishes to the SAME cache path reused one temporary name, "
            f"so the name is derived rather than unpredictable: {seen}"
        )

    def test_a_symlink_swapped_in_after_creation_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exclusive open guards creation only; the rest must use the fd.

        A local writer able to modify the cache directory can unlink the
        temporary between its creation and the calls that follow, then drop a
        symlink at the same name. A path-based `os.utime` then rewrites the
        LINK TARGET's timestamp and `os.replace` publishes the link itself as
        the hash cache, with the publish still reporting success (#1701
        review, verified exploitable).

        Both assertions are needed and neither implies the other: stamping
        through the descriptor stops the timestamp write, and the inode check
        stops the symlink being published. A fix doing only the first still
        installs the link.
        """
        cache_path = tmp_path / "cache.json"
        victim = tmp_path / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        before = victim.stat().st_mtime

        real_open = graph_updater_module._open_exclusive_temp

        def _swap_after_create(path: Path) -> tuple[int, str]:
            fd, name = real_open(path)
            os.unlink(name)
            os.symlink(victim, name)
            return fd, name

        monkeypatch.setattr(
            graph_updater_module, "_open_exclusive_temp", _swap_after_create
        )

        published = graph_updater_module._publish_hash_cache(
            cache_path, {"a.py": "deadbeef"}, 1_000_000.0
        )

        assert victim.stat().st_mtime == before, (
            "the stamp followed a symlink swapped in after creation and "
            "rewrote the victim's timestamp"
        )
        assert not cache_path.is_symlink(), (
            "a symlink swapped in after creation was published as the cache"
        )
        assert not published, (
            "a publish that could not use the file it created must report "
            "failure, not success over whatever replaced it"
        )

    def test_the_path_stamp_branch_stamps_and_stays_safe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates the platform where `os.utime` takes no descriptor.

        Windows has no `os.utime` in `os.supports_fd`, so passing a file
        descriptor raises `TypeError` and every hash-cache publish fails with
        it. This host is POSIX and always takes the fd branch, so that break
        was invisible locally until CI's Windows job ran (#1701 review).

        Asserts BOTH properties of the fallback, because the two orderings
        that satisfy them are different and I traded one for the other once:
        the stamp must land (durability, and `_is_already_in_sync` reads that
        mtime), and it must not follow a symlink swapped in after creation
        (safety, which requires it to run after the inode check).
        """
        monkeypatch.setattr(
            os, "supports_fd", frozenset(x for x in os.supports_fd if x is not os.utime)
        )
        assert os.utime not in os.supports_fd, "fixture guard: branch not forced"

        cache_path = tmp_path / "cache.json"
        victim = tmp_path / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        before = victim.stat().st_mtime

        real_open = graph_updater_module._open_exclusive_temp

        def _swap_after_create(path: Path) -> tuple[int, str]:
            fd, name = real_open(path)
            os.unlink(name)
            os.symlink(victim, name)
            return fd, name

        # First: the ordinary path, which must stamp.
        assert graph_updater_module._publish_hash_cache(
            cache_path, {"a.py": "x"}, 1_000_000.0
        )
        assert int(cache_path.stat().st_mtime) == 1_000_000, (
            "the fallback branch did not apply the timestamp, so a later run "
            "reads the write time and may skip edits made during this one"
        )

        # Then: the same branch under the post-creation swap.
        monkeypatch.setattr(
            graph_updater_module, "_open_exclusive_temp", _swap_after_create
        )
        published = graph_updater_module._publish_hash_cache(
            tmp_path / "cache2.json", {"a.py": "y"}, 2_000_000.0
        )
        assert victim.stat().st_mtime == before, (
            "the fallback stamp ran before the inode check and rewrote a "
            "swapped symlink target's timestamp"
        )
        assert not published, "a publish over a swapped path must refuse"

    def test_the_windows_stamp_lands_and_refuses_a_swapped_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows-shaped: `os.utime` in NEITHER capability set, `os.name == "nt"`.

        An earlier revision refused the publish whenever `os.utime` could not
        take a descriptor or `follow_symlinks=False`, on the belief that every
        CI platform had one of the two. Windows has neither (#1701 local
        review), so that revision would have declined every stamped publish
        there and brought back the 39 red tests it set out to fix. On Windows
        the stamp goes through the path, so this pins both halves: the
        ordinary publish lands with the stamp applied, and a link swapped in
        after creation is refused by the `S_ISLNK` check before the path
        stamp can follow it.

        The module's `_WINDOWS` flag is forced too, because the branch is
        keyed on it (`os.name` itself cannot be forced: `pathlib.Path()` reads
        it at call time and would refuse to build paths on this host). On this
        POSIX host a path-based `os.utime` DOES follow a symlink, which is
        exactly what makes the swap half of this test meaningful here.
        """
        monkeypatch.setattr(
            os, "supports_fd", frozenset(x for x in os.supports_fd if x is not os.utime)
        )
        monkeypatch.setattr(
            os,
            "supports_follow_symlinks",
            frozenset(x for x in os.supports_follow_symlinks if x is not os.utime),
        )
        monkeypatch.setattr(graph_updater_module, "_WINDOWS", True)
        assert os.utime not in os.supports_fd
        assert os.utime not in os.supports_follow_symlinks

        cache_path = tmp_path / "cache.json"
        assert graph_updater_module._publish_hash_cache(
            cache_path, {"a.py": "x"}, 1_000_000.0
        ), "the Windows-shaped publish was declined"
        assert int(cache_path.stat().st_mtime) == 1_000_000, (
            "the Windows-shaped publish landed without its stamp"
        )

        victim = tmp_path / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        before = victim.stat().st_mtime
        real_open = graph_updater_module._open_exclusive_temp

        def _swap_after_create(path: Path) -> tuple[int, str]:
            fd, name = real_open(path)
            os.unlink(name)
            os.symlink(victim, name)
            return fd, name

        monkeypatch.setattr(
            graph_updater_module, "_open_exclusive_temp", _swap_after_create
        )
        published = graph_updater_module._publish_hash_cache(
            tmp_path / "cache2.json", {"a.py": "y"}, 2_000_000.0
        )
        assert not published, "a publish over a swapped link must refuse"
        assert victim.stat().st_mtime == before, (
            "the Windows path stamp followed a swapped symlink to the victim"
        )
        assert not (tmp_path / "cache2.json").exists()

    def test_a_stamp_that_cannot_refuse_to_follow_declines_the_publish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No descriptor stamp, no `follow_symlinks=False`, and not the Windows branch.

        The previous fallback stamped through the PATH when neither was
        available, and forcing that branch showed a link swapped in after the
        inode check getting its target's timestamp rewritten (#1701 review,
        measured). Windows has its own branch (the test above); any other
        platform in that position has no safe way to stamp, so the publish
        must decline and leave the previous cache untouched rather than
        mis-stamp: a declined publish costs one re-hash, a mis-stamped one
        loses edits.

        Replacing `os.utime` is what forces the branch: the replacement is a
        member of neither `os.supports_fd` nor `os.supports_follow_symlinks`,
        and it records every call so the test can assert there were none.
        """
        # Forced off rather than asserted: the decline branch is keyed on the
        # module flag, not on the host, so it can and should run on the
        # Windows job too (a guard here went red there, #1701 CI).
        monkeypatch.setattr(graph_updater_module, "_WINDOWS", False)
        stamps: list[tuple] = []
        monkeypatch.setattr(
            graph_updater_module.os, "utime", lambda *a, **k: stamps.append((a, k))
        )
        assert graph_updater_module.os.utime not in os.supports_fd, (
            "fixture guard: the descriptor branch was not disabled"
        )
        assert graph_updater_module.os.utime not in os.supports_follow_symlinks, (
            "fixture guard: the no-follow branch was not disabled"
        )

        cache_path = tmp_path / "cache.json"
        cache_path.write_text('{"kept.py": "old"}', encoding="utf-8")
        victim = tmp_path / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        before = victim.stat().st_mtime

        real_open = graph_updater_module._open_exclusive_temp

        def _swap_after_create(path: Path) -> tuple[int, str]:
            fd, name = real_open(path)
            os.unlink(name)
            os.symlink(victim, name)
            return fd, name

        # The ordinary path first: with no safe stamp there is no publish.
        published = graph_updater_module._publish_hash_cache(
            cache_path, {"a.py": "x"}, 1_000_000.0
        )
        assert not published, "a publish that could not stamp safely reported success"
        assert json.loads(cache_path.read_text(encoding="utf-8")) == {
            "kept.py": "old"
        }, "a declined publish must leave the previous cache intact"
        assert stamps == [], (
            f"the path-following stamp ran on a platform that cannot refuse "
            f"to follow: {stamps}"
        )

        # Then under the swap, which is the case the old fallback got wrong.
        monkeypatch.setattr(
            graph_updater_module, "_open_exclusive_temp", _swap_after_create
        )
        published = graph_updater_module._publish_hash_cache(
            tmp_path / "cache2.json", {"a.py": "y"}, 2_000_000.0
        )
        assert not published
        assert victim.stat().st_mtime == before, (
            "the stamp followed a swapped symlink to the victim"
        )
        assert stamps == [], f"utime was called: {stamps}"
        assert not (tmp_path / "cache2.json").exists(), (
            "a declined publish left a cache behind"
        )

        # And a publish that has nothing to stamp still goes through: the
        # refusal is about the stamp, not about the platform.
        monkeypatch.setattr(graph_updater_module, "_open_exclusive_temp", real_open)
        assert graph_updater_module._publish_hash_cache(
            tmp_path / "cache3.json", {"a.py": "z"}, None
        ), "an unstamped publish must not be refused"
