"""Issue #2427: cgr keeps its state out of the user's working tree.

Every sync wrote its incremental state into the root of the indexed checkout
(`.cgr-hash-cache.json`, `.cgr-dir-mtimes.json`, `.cgr-exclusion-state.json`,
`.cgr-parser-fingerprint`, ...), and every edit its history and lock. A clean
`git status` became four to six untracked files, `git add -A` committed
machine-local state, a committed state file showed as modified after every
sync and was re-parsed by `cgr check`, and a read-only checkout could not be
synced incrementally.

The state now lives under CGR_HOME, one directory per checkout. What an
older cgr left in the tree is moved there on the next sync, so upgrading
keeps the hash cache: the first sync after it is still "already in sync".
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.checkout_state import prepare_state_dir, state_dir, state_file
from codebase_rag.cli import _delete_hash_cache
from codebase_rag.config import settings
from codebase_rag.editing.transaction import EditTransaction, load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import changed_since
from evals.cgr_graph import _StatefulIngestor

SOURCES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "from pkg.util import helper\n\n\ndef run():\n    return helper()\n",
    "pkg/util.py": "def helper():\n    return 1\n",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
    ).stdout


def _write_sources(root: Path) -> None:
    for rel, text in SOURCES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "statefiles"
    root.mkdir()
    _write_sources(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _sync(repo: Path, ingestor: MagicMock | _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run()
    return updater


def _state_in_tree(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith(".cgr-"))


def _as_an_older_cgr_left_it(repo: Path) -> dict[str, bytes]:
    """Put the checkout's state back in its root, where cgr kept it before.

    `os.replace` keeps each file's mtime, as the in-sync check requires of
    a hash cache an older cgr wrote. Wherever the sync put the files, they
    end in the tree, so this is the pre-upgrade layout either way.
    """
    directory = state_dir(repo)
    for name in cs.CGR_STATE_FILENAMES:
        if (directory / name).is_file():
            os.replace(directory / name, repo / name)
    if directory.is_dir():
        directory.rmdir()
    return {
        name: (repo / name).read_bytes()
        for name in cs.CGR_STATE_FILENAMES
        if (repo / name).is_file()
    }


# --- the working tree stays clean --------------------------------------------


def test_a_sync_leaves_git_status_clean(repo: Path, mock_ingestor: MagicMock) -> None:
    _sync(repo, mock_ingestor)

    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _state_in_tree(repo) == []


def test_the_next_sync_finds_its_state_under_cgr_home(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    _sync(repo, mock_ingestor)

    directory = state_dir(repo)
    assert directory.is_relative_to(settings.CGR_HOME)
    assert (directory / cs.HASH_CACHE_FILENAME).is_file()
    mock_ingestor.reset_mock()
    assert _sync(repo, mock_ingestor).skipped_because_in_sync is True


def test_an_edit_leaves_git_status_clean_and_keeps_its_history(repo: Path) -> None:
    # `cgr rename` and `cgr edits` added `.cgr-edit-history.json` and a
    # `.cgr-edit-lock` that stayed after the command.
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")
    assert tx.commit().applied

    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == (
        " M pkg/util.py\n"
    )
    assert len(load_history(repo)) == 1


@pytest.mark.skipif(
    sys.platform == cs.PLATFORM_WINDOWS or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the user running the tests",
)
def test_a_read_only_checkout_is_synced_incrementally(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    dirs = [repo, *(p for p in repo.rglob("*") if p.is_dir())]
    for directory in dirs:
        directory.chmod(0o555)
    try:
        _sync(repo, mock_ingestor)
        mock_ingestor.reset_mock()
        second = _sync(repo, mock_ingestor)
    finally:
        for directory in dirs:
            directory.chmod(0o755)

    assert second.skipped_because_in_sync is True


def test_cgr_check_leaves_out_state_files_a_user_committed(repo: Path) -> None:
    # `git add -A` after a sync committed the state; every later sync
    # rewrote it, and `cgr check` then re-parsed it as an edit.
    (repo / cs.HASH_CACHE_FILENAME).write_text('{"pkg/util.py": "0"}')
    (repo / cs.DIR_MTIMES_FILENAME).write_text('{".": 1.0}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "state committed by accident")
    (repo / cs.HASH_CACHE_FILENAME).write_text('{"pkg/util.py": "1"}')
    (repo / cs.DIR_MTIMES_FILENAME).unlink()
    (repo / "pkg" / "util.py").write_text("def helper():\n    return 2\n")

    assert changed_since(repo, "HEAD") == (["pkg/util.py"], [])


def test_cgr_check_still_reports_the_ignore_file_and_sources(repo: Path) -> None:
    # Negative: only cgr's own state is left out, not `.cgrignore`.
    (repo / ".cgrignore").write_text("build/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ignore file")
    (repo / ".cgrignore").write_text("build/\ndist/\n")
    (repo / "pkg" / "core.py").unlink()

    assert changed_since(repo, "HEAD") == ([".cgrignore"], ["pkg/core.py"])


# --- upgrading from a cgr that kept its state in the tree --------------------


def test_an_upgrade_keeps_the_hash_cache_and_stays_in_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    _sync(repo, mock_ingestor)
    legacy = _as_an_older_cgr_left_it(repo)
    assert cs.HASH_CACHE_FILENAME in legacy
    mock_ingestor.reset_mock()

    upgraded = _sync(repo, mock_ingestor)

    assert upgraded.skipped_because_in_sync is True
    assert _state_in_tree(repo) == []
    assert (state_dir(repo) / cs.HASH_CACHE_FILENAME).read_bytes() == legacy[
        cs.HASH_CACHE_FILENAME
    ]


def test_an_upgrade_reparses_only_the_file_that_changed(repo: Path) -> None:
    # A graph that answers the sync's reads, so the run is truly incremental.
    store = _StatefulIngestor()
    _sync(repo, store)
    _as_an_older_cgr_left_it(repo)
    edited = repo / "pkg" / "core.py"
    edited.write_text(SOURCES["pkg/core.py"] + "# edited\n")
    later = (repo / cs.HASH_CACHE_FILENAME).stat().st_mtime + 5
    os.utime(edited, (later, later))

    upgraded = _sync(repo, store)

    assert upgraded._reparsed_file_keys == {"pkg/core.py"}
    assert _state_in_tree(repo) == []


def test_an_upgrade_keeps_the_edit_history(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")
    tx.commit()
    history = load_history(repo)
    _as_an_older_cgr_left_it(repo)

    assert load_history(repo) == history
    assert _state_in_tree(repo) == []


@pytest.mark.parametrize("name", sorted(cs.CGR_STATE_FILENAMES))
def test_a_sync_moves_every_state_file_out_of_the_tree(
    repo: Path, mock_ingestor: MagicMock, name: str
) -> None:
    (repo / name).write_text("{}")

    _sync(repo, mock_ingestor)

    assert _state_in_tree(repo) == []


@pytest.mark.parametrize("name", sorted(cs.CGR_STATE_FILENAMES))
def test_a_moved_state_file_keeps_its_bytes_and_mtime(repo: Path, name: str) -> None:
    left = repo / name
    left.write_bytes(b'{"kept": true}')
    os.utime(left, (1_700_000_000, 1_700_000_000))

    moved = state_file(repo, name)

    assert moved.read_bytes() == b'{"kept": true}'
    assert moved.stat().st_mtime == 1_700_000_000
    assert not left.exists()


def test_a_state_file_registered_later_is_moved_with_no_code_of_its_own(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lock or stamp added to CGR_STATE_FILENAMES later (the sync lock of
    # #2441, say) is migrated like the rest.
    later = ".cgr-registered-later"
    monkeypatch.setattr(cs, "CGR_STATE_FILENAMES", cs.CGR_STATE_FILENAMES | {later})
    (repo / later).write_text("x")

    directory = prepare_state_dir(repo)

    assert (directory / later).read_text() == "x"
    assert _state_in_tree(repo) == []


def test_every_cgr_state_filename_is_registered() -> None:
    # A state file left out of the set is neither kept out of the tree when
    # an older cgr left it there nor skipped by the walk.
    names = {
        value
        for key, value in vars(cs).items()
        if key.endswith("_FILENAME")
        and isinstance(value, str)
        and value.startswith(".cgr-")
    }

    assert names - cs.CGR_STATE_FILENAMES == set()


def _replace_fails_for(
    legacy: Path, error: OSError, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `os.replace` refuses the move out of the tree, as across filesystems
    # (EXDEV) or from a tree cgr may not change (EACCES); every other
    # replace, the copy's own included, goes through.
    real_replace = os.replace

    def replace(src: str | Path, dst: str | Path) -> None:
        if Path(src) == legacy:
            raise error
        real_replace(src, dst)

    monkeypatch.setattr("codebase_rag.checkout_state.os.replace", replace)


def _warnings_during(action: Callable[[], object]) -> list[str]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        action()
    finally:
        logger.remove(sink)
    return messages


def _left_in_tree(repo: Path) -> Path:
    left = repo / cs.HASH_CACHE_FILENAME
    left.write_bytes(b'{"kept": true}')
    os.utime(left, (1_700_000_000, 1_700_000_000))
    return left


def test_state_on_another_filesystem_is_copied_with_its_mtime(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CGR_HOME on another disk than the checkout: no rename, so the state is
    # copied, keeping the mtime the in-sync check compares files against.
    left = _left_in_tree(repo)
    _replace_fails_for(left, OSError(errno.EXDEV, "cross-device link"), monkeypatch)

    moved = state_file(repo, cs.HASH_CACHE_FILENAME)

    assert moved.read_bytes() == b'{"kept": true}'
    assert moved.stat().st_mtime == 1_700_000_000
    assert not left.exists()
    assert sorted(p.name for p in moved.parent.iterdir()) == [cs.HASH_CACHE_FILENAME]


def test_state_in_a_tree_cgr_cannot_change_is_copied_and_the_copy_stays_current(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A checkout that can be read but not changed: the state is copied out,
    # the tree's copy stays where it is, and a later run keeps the current
    # copy instead of rolling back to the one in the tree.
    left = _left_in_tree(repo)
    _replace_fails_for(left, PermissionError(errno.EACCES, "read-only"), monkeypatch)
    real_unlink = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if path == left:
            raise PermissionError(errno.EACCES, "read-only")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    moved = state_file(repo, cs.HASH_CACHE_FILENAME)
    moved.write_text('{"kept": "since the upgrade"}')
    prepare_state_dir(repo)

    assert left.read_bytes() == b'{"kept": true}'
    assert moved.read_text() == '{"kept": "since the upgrade"}'


def test_state_another_process_moved_first_is_not_moved_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two syncs of one checkout upgrading at once: the other one's rename
    # wins, and this one neither fails nor reports the file as its own.
    left = _left_in_tree(repo)
    target = state_dir(repo) / cs.HASH_CACHE_FILENAME
    real_replace = os.replace

    def raced(src: str | Path, dst: str | Path) -> None:
        if Path(src) == left:
            real_replace(src, dst)
            raise FileNotFoundError(errno.ENOENT, "moved by another sync")
        real_replace(src, dst)

    monkeypatch.setattr("codebase_rag.checkout_state.os.replace", raced)
    adopted: list[str] = []
    sink = logger.add(adopted.append, level="INFO", format="{message}")
    try:
        prepare_state_dir(repo)
    finally:
        logger.remove(sink)

    assert target.read_bytes() == b'{"kept": true}'
    assert not left.exists()
    moved_note = ls.CHECKOUT_STATE_ADOPTED.split("{", 1)[0]
    assert not any(m.startswith(moved_note) for m in adopted), adopted


def test_a_failed_copy_keeps_the_state_in_the_tree_and_leaves_no_temp_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CGR_HOME fills up mid-copy: the tree's copy is all there is, so it
    # stays, and the half-written temp file does not.
    left = _left_in_tree(repo)
    _replace_fails_for(left, OSError(errno.EXDEV, "cross-device link"), monkeypatch)

    def disk_full(src: Path, dst: Path) -> None:
        Path(dst).write_bytes(b"{")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("codebase_rag.checkout_state.shutil.copy2", disk_full)

    warnings = _warnings_during(lambda: prepare_state_dir(repo))

    assert left.read_bytes() == b'{"kept": true}'
    assert list(state_dir(repo).iterdir()) == []
    failed = ls.CHECKOUT_STATE_ADOPT_FAILED.split("{", 1)[0]
    assert any(w.startswith(failed) for w in warnings), warnings


# --- negative: what stays as it was -------------------------------------------


def test_the_state_under_cgr_home_wins_over_a_stale_copy_in_the_tree(
    repo: Path,
) -> None:
    # A checkout of a commit that tracked the cache puts an old one back.
    kept = state_file(repo, cs.HASH_CACHE_FILENAME)
    kept.write_text('{"pkg/util.py": "current"}')
    (repo / cs.HASH_CACHE_FILENAME).write_text('{"pkg/util.py": "stale"}')

    prepare_state_dir(repo)

    assert kept.read_text() == '{"pkg/util.py": "current"}'
    assert _state_in_tree(repo) == []


@pytest.mark.skipif(sys.platform == cs.PLATFORM_WINDOWS, reason="symlinks")
def test_a_link_under_a_state_file_name_is_neither_followed_nor_moved(
    repo: Path, tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not cgr's")
    (repo / cs.HASH_CACHE_FILENAME).symlink_to(outside)

    _sync(repo, mock_ingestor)

    assert outside.read_text() == "not cgr's"
    assert (repo / cs.HASH_CACHE_FILENAME).is_symlink()
    cache = state_dir(repo) / cs.HASH_CACHE_FILENAME
    assert cache.is_file()
    assert not cache.is_symlink()


def test_other_files_in_the_tree_are_left_alone(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    (repo / ".cgrignore").write_text("build/\n")
    (repo / ".cgr-notes.md").write_text("mine\n")

    _sync(repo, mock_ingestor)

    assert (repo / ".cgrignore").read_text() == "build/\n"
    assert (repo / ".cgr-notes.md").read_text() == "mine\n"


def test_two_checkouts_with_one_name_keep_separate_state(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    first = tmp_path / "a" / "app"
    second = tmp_path / "b" / "app"
    for root in (first, second):
        root.mkdir(parents=True)
        _write_sources(root)

    _sync(first, mock_ingestor)

    assert state_dir(first) != state_dir(second)
    assert _sync(second, mock_ingestor).skipped_because_in_sync is False


@pytest.mark.skipif(sys.platform == cs.PLATFORM_WINDOWS, reason="symlinks")
def test_a_checkout_reached_through_a_link_shares_its_state(
    repo: Path, tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    _sync(repo, mock_ingestor)

    assert state_dir(link) == state_dir(repo)
    assert _sync(link, mock_ingestor).skipped_because_in_sync is True


def test_an_unwritable_cgr_home_neither_fails_the_sync_nor_writes_into_the_tree(
    repo: Path,
    tmp_path: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = tmp_path / "home-is-a-file"
    blocked.write_text("")
    monkeypatch.setattr(settings, "CGR_HOME", blocked)
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        _sync(repo, mock_ingestor)
    finally:
        logger.remove(sink)

    assert _state_in_tree(repo) == []
    unavailable = ls.CHECKOUT_STATE_DIR_UNAVAILABLE.split("{", 1)[0]
    assert any(w.startswith(unavailable) for w in warnings), warnings


def test_a_single_file_run_still_finds_the_root_a_sync_indexed(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    # The hash cache is the evidence of which ancestor indexed a file; it
    # no longer sits in that ancestor, and no `.git` stands in for it here.
    root = tmp_path / "plain"
    root.mkdir()
    _write_sources(root)
    _sync(root, mock_ingestor)

    parsers, queries = load_parsers()
    single = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=root / "pkg" / "util.py",
        parsers=parsers,
        queries=queries,
    )

    assert single.repo_path == root.resolve()


def test_a_cache_an_older_cgr_left_in_the_tree_still_marks_the_root(
    tmp_path: Path,
) -> None:
    # Negative: a single-file run before the first sync after upgrading
    # still roots where the older cgr indexed.
    root = tmp_path / "plain"
    root.mkdir()
    _write_sources(root)
    (root / cs.HASH_CACHE_FILENAME).write_text("{}")

    parsers, queries = load_parsers()
    single = GraphUpdater(
        ingestor=MagicMock(),
        repo_path=root / "pkg" / "util.py",
        parsers=parsers,
        queries=queries,
    )

    assert single.repo_path == root.resolve()


def test_clean_drops_the_sync_state_but_keeps_the_edit_history(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    _sync(repo, mock_ingestor)
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")
    tx.commit()

    _delete_hash_cache(repo)

    directory = state_dir(repo)
    assert not (directory / cs.HASH_CACHE_FILENAME).exists()
    assert not (directory / cs.DIR_MTIMES_FILENAME).exists()
    assert len(load_history(repo)) == 1
