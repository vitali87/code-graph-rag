"""Issue #2834: a change that lands with an older mtime is still synced.

Both fast paths treated a file as unchanged when its mtime was at or below
the hash cache's own write time, so content written by `cp -p`, `rsync -a`,
`tar -x`, `unzip` or a sync tool, all of which keep the source's timestamp,
was never hashed: every later sync said "already in sync" and the graph kept
definitions the file no longer had. The inode change time cannot be set
from user space and moves on every write, so it marks those files.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import (
    GraphUpdater,
    _cached_file_unchanged,
    _FileScan,
    _hash_file,
    _HashBaseline,
)
from codebase_rag.parser_loader import load_parsers

# The previous sync's cache stamp, and how far before it the copied file's
# preserved mtime sits.
CACHE_AGE_S = 600.0
PRESERVED_AGE_S = 1200.0
STATE_FILES = (
    cs.HASH_CACHE_FILENAME,
    cs.DIR_MTIMES_FILENAME,
    cs.PARSER_FINGERPRINT_FILENAME,
    cs.EXCLUSION_STATE_FILENAME,
)


def _updater(repo: Path, ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    )


@pytest.fixture
def synced(temp_repo: Path, mock_ingestor: MagicMock) -> tuple[Path, float]:
    (temp_repo / "__init__.py").touch()
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    (temp_repo / "module_b.py").write_text("def func_b():\n    pass\n")
    updater = _updater(temp_repo, mock_ingestor)
    updater.run()
    # Pin the last sync in the past, so every write below is after it. Every
    # state file moves together: a hash cache out of step with the directory
    # mtimes reads as an interrupted publish and forces a full rebuild.
    stamp = time.time() - CACHE_AGE_S
    for name in STATE_FILES:
        if (state := updater.state_dir / name).exists():
            os.utime(state, (stamp, stamp))
    return temp_repo, stamp


def _processed(repo: Path, ingestor: MagicMock) -> set[str]:
    """The files a sync of `repo` re-parses."""
    updater = _updater(repo, ingestor)
    with patch.object(
        updater, "_process_single_file", wraps=updater._process_single_file
    ) as spy:
        updater.run()
    return {Path(call.args[0]).name for call in spy.call_args_list}


def _copy_preserving_mtime(path: Path, text: str, stamp: float) -> None:
    # What `cp -p` / `rsync -a` leave: new bytes, an mtime from before the
    # last sync, and an inode change time of now.
    path.write_text(text)
    preserved = stamp - PRESERVED_AGE_S
    os.utime(path, (preserved, preserved))


def test_a_backdated_change_is_not_already_in_sync(
    synced: tuple[Path, float], mock_ingestor: MagicMock
) -> None:
    repo, stamp = synced
    _copy_preserving_mtime(repo / "module_a.py", "def renamed():\n    pass\n", stamp)

    assert _updater(repo, mock_ingestor)._is_already_in_sync() is False


def test_a_backdated_change_is_reparsed(
    synced: tuple[Path, float], mock_ingestor: MagicMock
) -> None:
    repo, stamp = synced
    _copy_preserving_mtime(repo / "module_a.py", "def renamed():\n    pass\n", stamp)

    assert "module_a.py" in _processed(repo, mock_ingestor)


def _baseline(key: str, old_hash: str, cache_mtime: float) -> _HashBaseline:
    return _HashBaseline(
        old_hashes={key: old_hash},
        pristine_hashes={key: old_hash},
        forced_reparse_keys=set(),
        reindex_all=False,
        is_full_build=False,
        cache_mtime=cache_mtime,
    )


def test_the_per_file_scan_hashes_a_backdated_change(
    synced: tuple[Path, float], mock_ingestor: MagicMock
) -> None:
    # A run past the whole-tree check settles each cached file through the
    # same shortcut, so a copied file beside an ordinary edit was kept too.
    repo, stamp = synced
    target = repo / "module_b.py"
    old_hash = _hash_file(target)
    _copy_preserving_mtime(target, "def copied():\n    pass\n", stamp)
    updater = _updater(repo, mock_ingestor)

    settled = updater._scan_mtime_fast_path(
        target, target.name, _baseline(target.name, old_hash, stamp), _FileScan()
    )

    assert settled is False


def test_the_whole_tree_check_hashes_a_backdated_change(
    synced: tuple[Path, float],
) -> None:
    repo, stamp = synced
    target = repo / "module_b.py"
    old_hash = _hash_file(target)
    _copy_preserving_mtime(target, "def copied():\n    pass\n", stamp)

    assert _cached_file_unchanged(str(target), old_hash, stamp) is False


# Negative: what must not change.


def test_an_untouched_tree_is_still_in_sync(
    synced: tuple[Path, float], mock_ingestor: MagicMock
) -> None:
    repo, _stamp = synced

    assert _updater(repo, mock_ingestor)._is_already_in_sync() is True


def test_a_metadata_only_change_is_still_in_sync(
    synced: tuple[Path, float], mock_ingestor: MagicMock
) -> None:
    # chmod moves the inode change time but not the bytes: the file is
    # hashed, and the hash still matches.
    repo, _stamp = synced
    target = repo / "module_a.py"
    target.chmod(0o644 if target.stat().st_mode & 0o100 else 0o755)

    assert _updater(repo, mock_ingestor)._is_already_in_sync() is True


def test_a_file_untouched_since_the_cache_is_still_settled_unhashed(
    synced: tuple[Path, float], mock_ingestor: MagicMock
) -> None:
    # The shortcut itself stays: a cache stamped after the file's last
    # change of any kind settles it without reading it.
    repo, _stamp = synced
    target = repo / "module_b.py"
    after_every_change = time.time() + CACHE_AGE_S
    scan = _FileScan()
    updater = _updater(repo, mock_ingestor)

    settled = updater._scan_mtime_fast_path(
        target,
        target.name,
        _baseline(target.name, "not-the-hash", after_every_change),
        scan,
    )

    assert settled is True
    assert scan.skipped_count == 1
    assert _cached_file_unchanged(str(target), "not-the-hash", after_every_change)
