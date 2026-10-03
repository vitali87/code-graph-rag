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
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.checkout_state import prepare_state_dir, state_dir, state_file
from codebase_rag.cli import _delete_hash_cache
from codebase_rag.config import settings
from codebase_rag.editing.transaction import (
    EditTransaction,
    TransactionOutcome,
    _lock_file,
    _unlock_file,
    load_history,
)
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


def test_cgr_check_reports_tracked_sources_that_only_share_the_state_prefix(
    repo: Path,
) -> None:
    # A source file is only cgr's state if it has a state file's name: a
    # tracked `.cgr-custom.py` is indexed as Python, so editing or deleting
    # it is a structural change the check must report (Greptile, PR #2557).
    (repo / ".cgr-custom.py").write_text("def kept():\n    return 1\n")
    (repo / "pkg" / ".cgr-helpers.py").write_text("def gone():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "sources named like state")
    (repo / ".cgr-custom.py").write_text("def renamed():\n    return 1\n")
    (repo / "pkg" / ".cgr-helpers.py").unlink()

    assert changed_since(repo, "HEAD") == (
        [".cgr-custom.py"],
        ["pkg/.cgr-helpers.py"],
    )


def test_cgr_check_reports_a_new_source_that_only_shares_the_state_prefix(
    repo: Path,
) -> None:
    (repo / ".cgr-added.py").write_text("def added():\n    return 1\n")

    assert changed_since(repo, "HEAD") == ([".cgr-added.py"], [])


@pytest.mark.parametrize("name", sorted(cs.CGR_STATE_FILENAMES))
def test_cgr_check_leaves_out_every_state_file_tracked_or_not(
    repo: Path, name: str
) -> None:
    # Negative: the exact names stay out, committed and edited, committed
    # and deleted, in a nested project's directory, or left untracked.
    (repo / name).write_text("{}")
    (repo / "pkg" / name).write_text("{}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "state committed by accident")
    (repo / name).write_text('{"rewritten": true}')
    (repo / "pkg" / name).unlink()
    (repo / "pkg" / "sub").mkdir()
    (repo / "pkg" / "sub" / name).write_text("{}")

    assert changed_since(repo, "HEAD") == ([], [])


@pytest.mark.parametrize(
    "leftover",
    [
        f"{cs.HASH_CACHE_FILENAME}.0123456789abcdef{cs.TMP_EXTENSION}",
        f"{cs.EDIT_HISTORY_FILENAME}{cs.TMP_EXTENSION}",
    ],
)
def test_cgr_check_leaves_out_a_temp_file_an_older_cgr_left_beside_its_state(
    repo: Path, leftover: str
) -> None:
    # An older cgr wrote its state through a temp sibling in the tree; one a
    # crash left behind is cgr's too, as the prefix filter used to say.
    (repo / leftover).write_text("{")

    assert changed_since(repo, "HEAD") == ([], [])


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
    # The older cgr's lock stays where an older cgr still running takes it.
    assert _state_in_tree(repo) == [cs.EDIT_LOCK_FILENAME]


_MOVED = sorted(cs.CGR_STATE_FILENAMES - cs.CGR_STATE_LOCK_FILENAMES)


@pytest.mark.parametrize("name", _MOVED)
def test_a_sync_moves_every_state_file_out_of_the_tree(
    repo: Path, mock_ingestor: MagicMock, name: str
) -> None:
    (repo / name).write_text("{}")

    _sync(repo, mock_ingestor)

    assert _state_in_tree(repo) == []


@pytest.mark.parametrize("name", _MOVED)
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
    # A stamp or marker added to CGR_STATE_FILENAMES later is migrated like
    # the rest (a lock, such as the sync lock of #2441, is registered in
    # CGR_STATE_LOCK_FILENAMES too and stays put).
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
    # Every move copies now, so this is the same path as on one disk.
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
    # Two syncs of one checkout upgrading at once: the other one's move
    # wins, and this one neither fails, warns nor reports the file as its
    # own.
    left = _left_in_tree(repo)
    target = state_dir(repo) / cs.HASH_CACHE_FILENAME
    real_copy2 = shutil.copy2

    def raced(src: str | Path, dst: str | Path) -> object:
        if Path(src) == left:
            os.replace(left, target)
        return real_copy2(src, dst)

    monkeypatch.setattr("codebase_rag.checkout_state.shutil.copy2", raced)
    notes: list[str] = []
    sink = logger.add(notes.append, level="INFO", format="{message}")
    try:
        prepare_state_dir(repo)
    finally:
        logger.remove(sink)

    assert target.read_bytes() == b'{"kept": true}'
    assert not left.exists()
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]
    moved_note = ls.CHECKOUT_STATE_ADOPTED.split("{", 1)[0]
    failed = ls.CHECKOUT_STATE_ADOPT_FAILED.split("{", 1)[0]
    assert not any(n.startswith((moved_note, failed)) for n in notes), notes


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


# --- a move never replaces state published meanwhile (Greptile, PR #2557) -----

_CURRENT = b'{"pkg/util.py": "current"}'


def _published_after_the_check(
    target: Path, monkeypatch: pytest.MonkeyPatch
) -> list[Path]:
    """A sync publishes `target` right after the move found it absent.

    The check is answered as it was when made, and the publication lands
    before the move goes on, which is the interleaving two processes reach.
    """
    real_exists = Path.exists
    fired: list[Path] = []

    def exists(self: Path, *args: object, **kwargs: object) -> bool:
        if self == target and not fired:
            fired.append(self)
            target.write_bytes(_CURRENT)
            return False
        return real_exists(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "exists", exists)
    return fired


def _no_hard_links(monkeypatch: pytest.MonkeyPatch) -> None:
    # CGR_HOME on a filesystem without hard links (FAT, some network shares).
    def link(src: object, dst: object, **kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("codebase_rag.checkout_state.os.link", link)


def test_state_a_sync_published_after_the_check_is_never_replaced(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = _left_in_tree(repo)
    target = state_dir(repo) / cs.HASH_CACHE_FILENAME
    fired = _published_after_the_check(target, monkeypatch)

    prepare_state_dir(repo)

    assert fired, "fixture guard: the move must have checked the target"
    assert target.read_bytes() == _CURRENT
    assert not left.exists()
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_state_published_while_copying_across_filesystems_is_never_replaced(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = _left_in_tree(repo)
    _replace_fails_for(left, OSError(errno.EXDEV, "cross-device link"), monkeypatch)
    target = state_dir(repo) / cs.HASH_CACHE_FILENAME
    fired = _published_after_the_check(target, monkeypatch)

    prepare_state_dir(repo)

    assert fired, "fixture guard: the move must have checked the target"
    assert target.read_bytes() == _CURRENT
    assert not left.exists()
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_state_published_on_a_cgr_home_without_hard_links_is_never_replaced(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = _left_in_tree(repo)
    _no_hard_links(monkeypatch)
    target = state_dir(repo) / cs.HASH_CACHE_FILENAME
    fired = _published_after_the_check(target, monkeypatch)

    prepare_state_dir(repo)

    assert fired, "fixture guard: the move must have checked the target"
    assert target.read_bytes() == _CURRENT
    assert not left.exists()
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_state_is_moved_with_its_mtime_on_a_cgr_home_without_hard_links(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: with nothing racing it the move still happens there.
    left = _left_in_tree(repo)
    _no_hard_links(monkeypatch)

    moved = state_file(repo, cs.HASH_CACHE_FILENAME)

    assert moved.read_bytes() == b'{"kept": true}'
    assert moved.stat().st_mtime == 1_700_000_000
    assert not left.exists()
    assert sorted(p.name for p in moved.parent.iterdir()) == [moved.name]


def test_a_failed_exclusive_copy_keeps_the_state_in_the_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CGR_HOME without hard links fills up while the file is written in
    # place: the half-written file must not pass for the state (the next
    # run would then drop the tree's copy as stale).
    left = _left_in_tree(repo)
    _no_hard_links(monkeypatch)

    def disk_full(source: object, target: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("codebase_rag.checkout_state.shutil.copyfileobj", disk_full)

    warnings = _warnings_during(lambda: prepare_state_dir(repo))

    assert left.read_bytes() == b'{"kept": true}'
    assert list(state_dir(repo).iterdir()) == []
    failed = ls.CHECKOUT_STATE_ADOPT_FAILED.split("{", 1)[0]
    assert any(w.startswith(failed) for w in warnings), warnings


_PUBLISHED_HISTORY = b'{"entries": ["published by an edit"]}'


def test_a_failed_exclusive_copy_never_deletes_history_an_edit_published(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sync moves the history while an edit, which holds the edit lock the
    # sync does not take, publishes its own under the same name with
    # `os.replace`. The copy then fails: its cleanup must remove only the
    # file it created, never the history the edit just wrote (Greptile, PR
    # #2557). The publish lands once the copy has closed its file, the one
    # moment Windows lets another process replace it too.
    left = repo / cs.EDIT_HISTORY_FILENAME
    left.write_bytes(b'{"entries": ["from an older cgr"]}')
    _no_hard_links(monkeypatch)
    target = state_dir(repo) / cs.EDIT_HISTORY_FILENAME

    real_copystat = shutil.copystat

    def published_then_failed(source: Path, destination: Path, **kwargs: bool) -> None:
        if Path(destination) != target:
            # `copy2` stamping the temp copy beside the target.
            real_copystat(source, destination, **kwargs)
            return
        fresh = target.with_name(f"{target.name}.edit")
        fresh.write_bytes(_PUBLISHED_HISTORY)
        os.replace(fresh, target)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(
        "codebase_rag.checkout_state.shutil.copystat", published_then_failed
    )

    prepare_state_dir(repo)

    assert target.read_bytes() == _PUBLISHED_HISTORY
    # The tree's copy is kept, as after any failed move; the next run finds
    # the published history in place and drops it then.
    assert left.read_bytes() == b'{"entries": ["from an older cgr"]}'
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_a_move_that_loses_its_destination_keeps_the_state_in_the_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The state directory removed part-way (a `cgr clean` of CGR_HOME, say):
    # the tree's copy is still there, so it is not taken for moved.
    left = _left_in_tree(repo)

    def gone(src: object, dst: object, **kwargs: object) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr("codebase_rag.checkout_state.os.link", gone)

    warnings = _warnings_during(lambda: prepare_state_dir(repo))

    assert left.read_bytes() == b'{"kept": true}'
    assert list(state_dir(repo).iterdir()) == []
    failed = ls.CHECKOUT_STATE_ADOPT_FAILED.split("{", 1)[0]
    assert any(w.startswith(failed) for w in warnings), warnings


def test_a_fresh_install_has_nothing_to_move(repo: Path) -> None:
    # Negative: no state in the tree, nothing moved, nothing reported.
    notes: list[str] = []
    sink = logger.add(notes.append, level="DEBUG", format="{message}")
    try:
        directory = prepare_state_dir(repo)
    finally:
        logger.remove(sink)

    assert directory.is_dir()
    assert list(directory.iterdir()) == []
    assert _state_in_tree(repo) == []
    moved_note = ls.CHECKOUT_STATE_ADOPTED.split("{", 1)[0]
    assert not any(n.startswith(moved_note) for n in notes), notes


# --- the edit lock an older cgr takes in the tree (Greptile, PR #2557) --------


@contextmanager
def _an_older_cgr_holds_the_tree_lock(repo: Path) -> Iterator[Path]:
    """What an older cgr's `_repo_lock` does: lock `.cgr-edit-lock` at the
    checkout root, creating it if it is not there."""
    path = repo / cs.EDIT_LOCK_FILENAME
    with path.open("ab") as handle:
        _lock_file(handle)
        try:
            yield path
        finally:
            _unlock_file(handle)


def _commit_in_background(
    repo: Path, text: str
) -> tuple[threading.Thread, list[TransactionOutcome]]:
    outcome: list[TransactionOutcome] = []

    def commit() -> None:
        tx = EditTransaction(repo)
        tx.stage("pkg/util.py", text)
        outcome.append(tx.commit())

    worker = threading.Thread(target=commit, daemon=True)
    worker.start()
    return worker, outcome


def _waits(worker: threading.Thread) -> bool:
    worker.join(timeout=1.0)
    return worker.is_alive()


def _finishes(worker: threading.Thread) -> bool:
    worker.join(timeout=60)
    return not worker.is_alive()


def test_an_older_cgr_holding_the_tree_lock_holds_off_a_commit_across_filesystems(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CGR_HOME on another disk: a copy of the lock is another file, so a
    # commit that locked the copy ran beside the older cgr's.
    lock = repo / cs.EDIT_LOCK_FILENAME
    lock.touch()
    _replace_fails_for(lock, OSError(errno.EXDEV, "cross-device link"), monkeypatch)

    with _an_older_cgr_holds_the_tree_lock(repo):
        worker, outcome = _commit_in_background(repo, "def helper():\n    return 2\n")
        assert _waits(worker), "the commit ran while an older cgr held the lock"
        assert (repo / "pkg" / "util.py").read_text() == SOURCES["pkg/util.py"]

    assert _finishes(worker)
    assert outcome[0].applied


def test_an_older_cgr_still_running_after_the_upgrade_still_holds_off_commits(
    repo: Path,
) -> None:
    # Same filesystem: moving the lock kept its inode only for the first
    # commit. The older cgr then locked a new file at the old path, and the
    # next commit removed that one and ran beside it.
    (repo / cs.EDIT_LOCK_FILENAME).touch()
    first = EditTransaction(repo)
    first.stage("pkg/util.py", "def helper():\n    return 2\n")
    assert first.commit().applied

    with _an_older_cgr_holds_the_tree_lock(repo):
        worker, outcome = _commit_in_background(repo, "def helper():\n    return 3\n")
        assert _waits(worker), "the commit ran while an older cgr held the lock"

    assert _finishes(worker)
    assert outcome[0].applied


def test_an_older_cgr_finishing_its_edit_keeps_its_history_entry(repo: Path) -> None:
    # The older cgr is mid-edit when the upgraded one commits: it records
    # its entry in the tree's history before releasing the lock, and the
    # history moves only after that, so neither entry is lost.
    history = repo / cs.EDIT_HISTORY_FILENAME
    history.write_text(json.dumps([{cs.EDIT_KEY_ID: "before-upgrade"}]))

    with _an_older_cgr_holds_the_tree_lock(repo):
        worker, outcome = _commit_in_background(repo, "def helper():\n    return 2\n")
        assert _waits(worker), "the commit ran while an older cgr held the lock"
        entries = json.loads(history.read_text()) if history.is_file() else []
        history.write_text(json.dumps([*entries, {cs.EDIT_KEY_ID: "older-cgr"}]))

    assert _finishes(worker)
    assert [entry[cs.EDIT_KEY_ID] for entry in load_history(repo)] == [
        "before-upgrade",
        "older-cgr",
        outcome[0].transaction_id,
    ]


def test_the_lock_an_older_cgr_left_is_neither_moved_nor_removed(repo: Path) -> None:
    # Removing or replacing a lock file another process may hold or be about
    # to open is what breaks the exclusion, so it stays where it is.
    lock = repo / cs.EDIT_LOCK_FILENAME
    lock.touch()
    before = lock.stat()

    prepare_state_dir(repo)
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")
    assert tx.commit().applied

    assert lock.stat().st_ino == before.st_ino
    assert _state_in_tree(repo) == [cs.EDIT_LOCK_FILENAME]


def test_a_directory_named_like_the_tree_lock_is_not_taken(repo: Path) -> None:
    # Only a regular file is an older cgr's lock; anything else by that name
    # is neither opened nor removed, and the commit goes ahead.
    (repo / cs.EDIT_LOCK_FILENAME).mkdir()
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")

    assert tx.commit().applied
    assert (repo / cs.EDIT_LOCK_FILENAME).is_dir()


def test_a_live_edit_lock_still_holds_off_a_second_editor(repo: Path) -> None:
    # Negative: with no older cgr around, the lock under CGR_HOME is the one
    # that excludes, and no lock is created in the tree.
    lock = prepare_state_dir(repo) / cs.EDIT_LOCK_FILENAME
    with lock.open("ab") as handle:
        _lock_file(handle)
        try:
            worker, outcome = _commit_in_background(
                repo, "def helper():\n    return 2\n"
            )
            assert _waits(worker), "the commit ran while another editor held it"
        finally:
            _unlock_file(handle)

    assert _finishes(worker)
    assert outcome[0].applied
    assert _state_in_tree(repo) == []


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


def test_a_reused_checkout_path_roots_a_nested_checkout_at_its_own_git(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    # The checkout at `root` is synced, then removed; its state stays under
    # CGR_HOME. A different tree at the same path holds a nested checkout
    # the old sync never saw, so its file roots at its own `.git`, and is
    # keyed `pkg/mod.py` rather than `lib/pkg/mod.py` under the old root
    # (Greptile, PR #2557).
    root = tmp_path / "reused"
    root.mkdir()
    _write_sources(root)
    _sync(root, mock_ingestor)
    shutil.rmtree(root)
    nested = root / "lib"
    (nested / "pkg").mkdir(parents=True)
    (nested / cs.GIT_DIR_NAME).mkdir()
    (nested / "pkg" / "mod.py").write_text("def run():\n    return 1\n")

    parsers, queries = load_parsers()
    single = GraphUpdater(
        ingestor=MagicMock(),
        repo_path=nested / "pkg" / "mod.py",
        parsers=parsers,
        queries=queries,
    )

    assert (state_dir(root) / cs.HASH_CACHE_FILENAME).is_file()
    assert single.repo_path == nested.resolve()


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
