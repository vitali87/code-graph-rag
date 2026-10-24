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

import base64
import errno
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import checkout_state
from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.checkout_state import (
    _lock_file,
    _unlock_file,
    prepare_state_dir,
    state_dir,
    state_file,
)
from codebase_rag.cli import _delete_hash_cache
from codebase_rag.config import settings
from codebase_rag.editing.transaction import (
    EditTransaction,
    StagedTree,
    TransactionConflict,
    TransactionOutcome,
    load_history,
    undo_last,
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
    end in the tree, so this is the pre-upgrade layout either way. The sync
    lock is dropped instead: no cgr that kept its state in the tree took
    one (issue #2441). So are the vector store stamps: none wrote those
    either.
    """
    directory = state_dir(repo)
    for name in cs.CGR_STATE_FILENAMES:
        if not (directory / name).is_file():
            continue
        if name == cs.SYNC_LOCK_FILENAME:
            (directory / name).unlink()
        else:
            os.replace(directory / name, repo / name)
    stamps = cs.EMBEDDINGS_STORE_PROJECT_TEMPLATE.format(digest="*")
    for stamp in directory.glob(stamps):
        stamp.unlink()
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
# The history is added entry by entry rather than moved as bytes.
_MOVED_AS_IS = sorted(set(_MOVED) - {cs.EDIT_HISTORY_FILENAME})


@pytest.mark.parametrize("name", _MOVED)
def test_a_sync_moves_every_state_file_out_of_the_tree(
    repo: Path, mock_ingestor: MagicMock, name: str
) -> None:
    (repo / name).write_text("{}")

    _sync(repo, mock_ingestor)

    assert _state_in_tree(repo) == []


@pytest.mark.parametrize("name", _MOVED_AS_IS)
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


_PUBLISHED = b'{"pkg/util.py": "published by a sync"}'


def test_a_failed_exclusive_copy_never_deletes_state_another_process_published(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # State published under the same name with `os.replace` while the move
    # copies it. The copy then fails: its cleanup must remove only the file
    # it created, never the one another process just wrote (Greptile, PR
    # #2557). The publish lands once the copy has closed its file, the one
    # moment Windows lets another process replace it too.
    left = _left_in_tree(repo)
    _no_hard_links(monkeypatch)
    target = state_dir(repo) / cs.HASH_CACHE_FILENAME

    real_copystat = shutil.copystat

    def published_then_failed(source: Path, destination: Path, **kwargs: bool) -> None:
        if Path(destination) != target:
            # `copy2` stamping the temp copy beside the target.
            real_copystat(source, destination, **kwargs)
            return
        fresh = target.with_name(f"{target.name}.sync")
        fresh.write_bytes(_PUBLISHED)
        os.replace(fresh, target)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(
        "codebase_rag.checkout_state.shutil.copystat", published_then_failed
    )

    prepare_state_dir(repo)

    assert target.read_bytes() == _PUBLISHED
    # The tree's copy is kept, as after any failed move; the next run finds
    # the published state in place and drops it then.
    assert left.read_bytes() == b'{"kept": true}'
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
    history.write_text(json.dumps([_a_recorded_entry("before-upgrade")]))

    with _an_older_cgr_holds_the_tree_lock(repo):
        worker, outcome = _commit_in_background(repo, "def helper():\n    return 2\n")
        assert _waits(worker), "the commit ran while an older cgr held the lock"
        entries = json.loads(history.read_text()) if history.is_file() else []
        history.write_text(json.dumps([*entries, _a_recorded_entry("older-cgr")]))

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


# --- an older cgr still editing after the upgrade (Greptile, #2490, #2550) ----

_UTIL_BY_AN_OLDER_CGR = "def helper():\n    return 7\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _replace_in_tree(path: Path, data: bytes) -> None:
    # An older cgr's `_write_file`: a temp sibling, then `os.replace`.
    temp = path.with_name(f"{path.name}{cs.TMP_EXTENSION}")
    temp.write_bytes(data)
    os.replace(temp, path)


class _OlderCgrEdit:
    """One edit by a cgr from before #2557, made while it holds the tree lock.

    `load` reads the history at the checkout root, `apply` writes the file
    and appends this edit's entry to the history it loaded, as that cgr's
    commit and `record_transaction` do.
    """

    def __init__(self, repo: Path, rel: str, text: str, tx_id: str) -> None:
        self.repo = repo
        self.rel = rel
        self.text = text
        self.tx_id = tx_id
        self.entries: list[dict] = []

    def load(self) -> None:
        history = self.repo / cs.EDIT_HISTORY_FILENAME
        self.entries = json.loads(history.read_text()) if history.is_file() else []

    def apply(self) -> None:
        target = self.repo / self.rel
        before = target.read_bytes()
        after = self.text.encode()
        _replace_in_tree(target, after)
        entry = {
            cs.EDIT_KEY_ID: self.tx_id,
            cs.EDIT_KEY_AT: datetime.now(UTC).isoformat(timespec="seconds"),
            cs.EDIT_KEY_FILES: [
                {
                    cs.KEY_PATH: self.rel,
                    cs.EDIT_KEY_BEFORE: _b64(before),
                    cs.EDIT_KEY_AFTER: _b64(after),
                    cs.EDIT_KEY_MODE: None,
                }
            ],
            cs.EDIT_KEY_VERIFICATION: {cs.EDIT_KEY_OK: True, cs.EDIT_KEY_MESSAGE: ""},
        }
        _replace_in_tree(
            self.repo / cs.EDIT_HISTORY_FILENAME,
            json.dumps([*self.entries, entry]).encode(),
        )


def _an_older_cgr_edits(repo: Path, rel: str, text: str, tx_id: str) -> None:
    edit = _OlderCgrEdit(repo, rel, text, tx_id)
    with _an_older_cgr_holds_the_tree_lock(repo):
        edit.load()
        edit.apply()


def _ids(repo: Path) -> list[str]:
    return [str(entry.get(cs.EDIT_KEY_ID)) for entry in load_history(repo)]


def test_an_edit_an_older_cgr_records_after_the_history_moved_can_be_undone(
    repo: Path,
) -> None:
    # The upgraded cgr has moved the history; an older cgr still running
    # (an MCP server started before the upgrade) then edits and starts a new
    # history at the root. The next sync dropped it as a stale copy, and
    # `cgr edits undo` reversed the upgraded cgr's edit instead.
    tx = EditTransaction(repo)
    tx.stage("pkg/core.py", "def run():\n    return 0\n")
    assert tx.commit().applied
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")

    prepare_state_dir(repo)

    assert _ids(repo) == [tx.transaction_id, "older-cgr"]


def test_undo_reverses_the_edit_an_older_cgr_made_last(repo: Path) -> None:
    tx = EditTransaction(repo)
    tx.stage("pkg/core.py", "def run():\n    return 0\n")
    assert tx.commit().applied
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")

    undone = undo_last(repo)

    assert [o.transaction_id for o in undone] == ["older-cgr"]
    assert (repo / "pkg" / "util.py").read_text() == SOURCES["pkg/util.py"]


def test_a_sync_while_an_older_cgr_edits_keeps_that_edits_history_entry(
    repo: Path,
) -> None:
    # Greptile's interleaving: the older cgr holds its lock and has read the
    # history when a sync moves it; it then writes its entry into a history
    # at the root, which the next sync dropped.
    _an_older_cgr_edits(repo, "pkg/core.py", "def run():\n    return 0\n", "earlier")
    edit = _OlderCgrEdit(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    with _an_older_cgr_holds_the_tree_lock(repo):
        edit.load()
        prepare_state_dir(repo)
        edit.apply()

    prepare_state_dir(repo)

    assert _ids(repo) == ["earlier", "older-cgr"]


def test_an_older_cgr_that_starts_editing_while_a_commit_verifies_is_not_overwritten(
    repo: Path,
) -> None:
    # No tree lock yet: the older cgr creates it, and edits the same file,
    # while the upgraded cgr's verifier runs. The commit then wrote over that
    # edit and recorded a `before` the file no longer had.
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")

    def verify(tree: StagedTree) -> bool:
        _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
        return True

    with pytest.raises(TransactionConflict):
        tx.commit(verify)
    assert (repo / "pkg" / "util.py").read_text() == _UTIL_BY_AN_OLDER_CGR


def test_a_commit_waits_for_an_older_cgr_that_locked_the_tree_while_it_verified(
    repo: Path,
) -> None:
    # The older cgr still holds the lock it created when the verifier
    # returns: the commit writes only once it is released.
    verifying = threading.Event()
    locked = threading.Event()

    def verify(tree: StagedTree) -> bool:
        verifying.set()
        return locked.wait(timeout=60)

    outcome: list[TransactionOutcome] = []

    def commit() -> None:
        tx = EditTransaction(repo)
        tx.stage("pkg/util.py", "def helper():\n    return 2\n")
        outcome.append(tx.commit(verify))

    worker = threading.Thread(target=commit, daemon=True)
    worker.start()
    assert verifying.wait(timeout=60)
    with _an_older_cgr_holds_the_tree_lock(repo):
        locked.set()
        assert _waits(worker), "the commit wrote while an older cgr held the lock"
        assert (repo / "pkg" / "util.py").read_text() == SOURCES["pkg/util.py"]

    assert _finishes(worker)
    assert outcome[0].applied


def test_a_verified_commit_with_no_older_cgr_creates_no_lock_in_the_tree(
    repo: Path,
) -> None:
    # Negative: looking for an older cgr's lock after verifying opens one
    # only if it is there; with none, the tree gets none.
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")

    assert tx.commit(lambda tree: True).applied
    assert _state_in_tree(repo) == []


def _an_upgraded_edit(repo: Path) -> str:
    tx = EditTransaction(repo)
    tx.stage("pkg/core.py", "def run():\n    return 0\n")
    assert tx.commit().applied
    return tx.transaction_id


def test_a_tree_history_with_a_list_id_neither_fails_the_sync_nor_is_added(
    repo: Path,
) -> None:
    # The history is data on disk: an id no cgr writes (unhashable here)
    # marks a copy that is not an older cgr's history, which is dropped as
    # a stale copy is rather than raising out of the sync (Codex, PR #3344).
    kept = _an_upgraded_edit(repo)
    (repo / cs.EDIT_HISTORY_FILENAME).write_text(
        json.dumps([{cs.EDIT_KEY_ID: ["not", "an", "id"]}])
    )

    prepare_state_dir(repo)

    assert _ids(repo) == [kept]
    assert _state_in_tree(repo) == []


def test_a_cgr_home_history_with_a_dict_id_neither_fails_the_sync_nor_is_rewritten(
    repo: Path,
) -> None:
    # A malformed history under CGR_HOME is never rewritten, and the older
    # cgr's history it cannot take stays in the tree rather than going.
    _an_upgraded_edit(repo)
    history = state_dir(repo) / cs.EDIT_HISTORY_FILENAME
    malformed = json.dumps([{cs.EDIT_KEY_ID: {"not": "an id"}}]).encode()
    history.write_bytes(malformed)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    left = (repo / cs.EDIT_HISTORY_FILENAME).read_bytes()

    prepare_state_dir(repo)

    assert history.read_bytes() == malformed
    assert (repo / cs.EDIT_HISTORY_FILENAME).read_bytes() == left


def test_a_deeply_nested_tree_history_does_not_fail_the_sync(repo: Path) -> None:
    kept = _an_upgraded_edit(repo)
    depth = 100_000
    (repo / cs.EDIT_HISTORY_FILENAME).write_text("[" * depth + "]" * depth)

    prepare_state_dir(repo)

    assert _ids(repo) == [kept]


def test_a_well_formed_older_history_is_added_entry_for_entry(repo: Path) -> None:
    # Negative: with string ids on both sides, the merge keeps every entry
    # of both copies as it was, the older cgr's last.
    _an_upgraded_edit(repo)
    kept = json.loads((state_dir(repo) / cs.EDIT_HISTORY_FILENAME).read_text())
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    recorded = json.loads((repo / cs.EDIT_HISTORY_FILENAME).read_text())

    prepare_state_dir(repo)

    assert load_history(repo) == [*kept, *recorded]


# Decodes, but the encoder that writes the history recurses deeper than it.
_NESTED = 1200


def _nested() -> list:
    value: list = []
    for _ in range(_NESTED):
        value = [value]
    return value


def test_a_tree_history_nested_too_deep_to_write_does_not_fail_the_sync(
    repo: Path,
) -> None:
    # Greptile, PR #3344: an entry nested deep enough still decodes, and
    # then failed the sync when the merged history was written.
    kept = _an_upgraded_edit(repo)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    left = repo / cs.EDIT_HISTORY_FILENAME
    entries = json.loads(left.read_text())
    entries[0][cs.EDIT_KEY_VERIFICATION] = _nested()
    left.write_text(json.dumps(entries))

    prepare_state_dir(repo)

    assert _ids(repo) == [kept]


def test_a_cgr_home_history_nested_too_deep_to_write_is_never_rewritten(
    repo: Path,
) -> None:
    _an_upgraded_edit(repo)
    history = state_dir(repo) / cs.EDIT_HISTORY_FILENAME
    entries = json.loads(history.read_text())
    entries[0][cs.EDIT_KEY_VERIFICATION] = _nested()
    malformed = json.dumps(entries).encode()
    history.write_bytes(malformed)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")

    prepare_state_dir(repo)

    assert history.read_bytes() == malformed
    assert (repo / cs.EDIT_HISTORY_FILENAME).is_file()


def _history_entry(tx_id: str, files: list) -> dict:
    return {cs.EDIT_KEY_ID: tx_id, cs.EDIT_KEY_FILES: files}


def _a_recorded_entry(tx_id: str) -> dict:
    """An entry as an older cgr recorded it, for a file in the checkout."""
    record = {
        cs.KEY_PATH: "pkg/__init__.py",
        cs.EDIT_KEY_BEFORE: _b64(b""),
        cs.EDIT_KEY_AFTER: _b64(f"# {tx_id}\n".encode()),
        cs.EDIT_KEY_MODE: None,
    }
    return _history_entry(tx_id, [record])


def test_an_older_history_entry_with_a_malformed_file_record_is_not_added(
    repo: Path,
) -> None:
    # CodeRabbit, PR #3344: undo reads every record of the newest entry, so
    # a record it cannot read would stop every earlier edit's undo.
    kept = _an_upgraded_edit(repo)
    (repo / cs.EDIT_HISTORY_FILENAME).write_text(
        json.dumps([_history_entry("bad", ["not-an-object"])])
    )

    prepare_state_dir(repo)

    assert _ids(repo) == [kept]


def test_an_older_history_entry_naming_a_path_outside_the_checkout_is_not_added(
    repo: Path,
) -> None:
    kept = _an_upgraded_edit(repo)
    record = {
        cs.KEY_PATH: "../outside.py",
        cs.EDIT_KEY_BEFORE: None,
        cs.EDIT_KEY_AFTER: _b64(b"x = 1\n"),
    }
    (repo / cs.EDIT_HISTORY_FILENAME).write_text(
        json.dumps([_history_entry("outside", [record])])
    )

    prepare_state_dir(repo)

    assert _ids(repo) == [kept]


def test_an_edit_an_older_cgr_appends_to_a_history_git_put_back_is_added(
    repo: Path,
) -> None:
    # CodeRabbit, PR #3344: git puts back the history [A] that moved (a
    # commit tracked it), and an older cgr appends its edit B to it. A is
    # already under CGR_HOME; B is not, and was the only record of its edit.
    kept = _an_upgraded_edit(repo)
    restored = (state_dir(repo) / cs.EDIT_HISTORY_FILENAME).read_bytes()
    (repo / cs.EDIT_HISTORY_FILENAME).write_bytes(restored)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")

    prepare_state_dir(repo)

    assert _ids(repo) == [kept, "older-cgr"]


def test_an_older_cgrs_edit_after_newer_entries_is_undone_first(repo: Path) -> None:
    # [A, X] under CGR_HOME and [A, B] in the tree: B came after X, so it
    # goes last, and undo reverses it before X.
    _an_upgraded_edit(repo)
    restored = (state_dir(repo) / cs.EDIT_HISTORY_FILENAME).read_bytes()
    tx = EditTransaction(repo)
    tx.stage("pkg/__init__.py", "VERSION = 2\n")
    assert tx.commit().applied
    (repo / cs.EDIT_HISTORY_FILENAME).write_bytes(restored)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")

    prepare_state_dir(repo)

    undone = [o.transaction_id for o in undo_last(repo, 2)]
    assert undone == ["older-cgr", tx.transaction_id]


@contextmanager
def _tree_history_cannot_be_removed(repo: Path) -> Iterator[None]:
    """Removing the tree's history fails (a Windows sharing violation, say)."""
    left = repo / cs.EDIT_HISTORY_FILENAME
    real_unlink = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if path == left:
            raise PermissionError(errno.EACCES, "in use")
        real_unlink(path, missing_ok=missing_ok)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        yield


def test_an_older_cgrs_edit_appended_to_a_history_left_behind_is_added(
    repo: Path,
) -> None:
    # Greptile, PR #3344: the history was added but could not be removed,
    # and an older cgr then appended its next edit to that copy.
    kept = _an_upgraded_edit(repo)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "first")
    with _tree_history_cannot_be_removed(repo):
        prepare_state_dir(repo)
    _an_older_cgr_edits(repo, "pkg/__init__.py", "VERSION = 2\n", "second")

    prepare_state_dir(repo)

    assert _ids(repo) == [kept, "first", "second"]


def test_an_undone_edit_in_a_history_left_behind_does_not_come_back(
    repo: Path,
) -> None:
    kept = _an_upgraded_edit(repo)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "undone")
    with _tree_history_cannot_be_removed(repo):
        prepare_state_dir(repo)
        undo_last(repo)
    _an_older_cgr_edits(repo, "pkg/__init__.py", "VERSION = 2\n", "later")

    prepare_state_dir(repo)

    assert _ids(repo) == [kept, "later"]


def test_an_edit_waits_for_an_older_cgrs_history_that_could_not_be_added(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Greptile, PR #3344: the older cgr's entry could not be added, and an
    # edit committed anyway. Added later, the older entry went after the
    # newer one, and undo reversed the wrong edit.
    kept = _an_upgraded_edit(repo)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    real_write = checkout_state.write_history
    failures = [OSError(errno.ENOSPC, "No space left on device")]

    def write_once_failing(repo_path: Path, entries: list[dict]) -> None:
        if failures:
            raise failures.pop()
        real_write(repo_path, entries)

    monkeypatch.setattr(checkout_state, "write_history", write_once_failing)
    tx = EditTransaction(repo)
    tx.stage("pkg/__init__.py", "VERSION = 2\n")

    with pytest.raises(OSError):
        tx.commit()
    assert (repo / "pkg" / "__init__.py").read_text() == ""
    assert (repo / cs.EDIT_HISTORY_FILENAME).is_file()

    retry = EditTransaction(repo)
    retry.stage("pkg/__init__.py", "VERSION = 2\n")
    assert retry.commit().applied
    assert _ids(repo) == [kept, "older-cgr", retry.transaction_id]


def _commit_util(repo: Path, text: str) -> str:
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", f"def helper():\n    {text}\n")
    assert tx.commit().applied
    return tx.transaction_id


def test_an_entry_aged_out_of_the_history_is_not_added_back_from_the_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CodeRabbit, PR #3344: the oldest entry dropped past the limit was not
    # settled, so a copy of the history in the tree that still held it put
    # it back as the newest entry.
    monkeypatch.setattr(cs, "EDIT_HISTORY_LIMIT", 2)
    aged = _commit_util(repo, "return 2")
    copy = (state_dir(repo) / cs.EDIT_HISTORY_FILENAME).read_bytes()
    _commit_util(repo, "return 3")
    _commit_util(repo, "return 4")
    (repo / cs.EDIT_HISTORY_FILENAME).write_bytes(copy)

    prepare_state_dir(repo)

    assert aged not in _ids(repo)


def test_an_entry_aged_out_while_adding_an_older_cgrs_is_not_added_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cs, "EDIT_HISTORY_LIMIT", 2)
    aged = _commit_util(repo, "return 2")
    copy = (state_dir(repo) / cs.EDIT_HISTORY_FILENAME).read_bytes()
    _an_upgraded_edit(repo)
    _an_older_cgr_edits(repo, "pkg/__init__.py", "VERSION = 2\n", "older-cgr")
    prepare_state_dir(repo)
    (repo / cs.EDIT_HISTORY_FILENAME).write_bytes(copy)

    prepare_state_dir(repo)

    assert aged not in _ids(repo)


def test_entries_within_the_history_limit_are_neither_dropped_nor_settled(
    repo: Path,
) -> None:
    # Negative: below the limit, nothing leaves the history and no id is
    # recorded as settled.
    ids = [_commit_util(repo, "return 2"), _commit_util(repo, "return 3")]

    assert _ids(repo) == ids
    assert not (state_dir(repo) / cs.EDIT_HISTORY_LEDGER_FILENAME).exists()


def _tree_lock_cannot_be_opened(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = repo / cs.EDIT_LOCK_FILENAME
    lock.touch()
    real_open = Path.open

    def refusing(path: Path, *args: str, **kwargs: str) -> IO:
        if path == lock:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refusing)


def test_an_edit_stops_when_an_older_cgrs_lock_cannot_be_opened(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CodeRabbit, PR #3344: a tree lock that is there but could not be
    # opened was taken for no lock at all, so the edit ran beside an older
    # cgr that may hold it.
    _tree_lock_cannot_be_opened(repo, monkeypatch)
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")

    with pytest.raises(OSError, match=cs.EDIT_LOCK_FILENAME):
        tx.commit()
    assert (repo / "pkg" / "util.py").read_text() == SOURCES["pkg/util.py"]
    assert load_history(repo) == []


def test_an_undo_stops_when_an_older_cgrs_lock_cannot_be_opened(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = _commit_util(repo, "return 2")
    _tree_lock_cannot_be_opened(repo, monkeypatch)

    with pytest.raises(OSError, match=cs.EDIT_LOCK_FILENAME):
        undo_last(repo)
    assert _ids(repo) == [edited]


def test_a_tree_lock_gone_before_it_is_opened_counts_as_none(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: only a lock that is there stops an edit. One removed between
    # the check and the open is no lock, as is none at all.
    lock = repo / cs.EDIT_LOCK_FILENAME
    lock.touch()
    real_open = Path.open

    def removed_first(path: Path, *args: str, **kwargs: str) -> IO:
        if path == lock:
            lock.unlink()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", removed_first)
    tx = EditTransaction(repo)
    tx.stage("pkg/util.py", "def helper():\n    return 2\n")

    assert tx.commit().applied


def test_migrating_a_history_already_added_adds_nothing(repo: Path) -> None:
    # Negative: the same copy put back again (by git, say) adds no entry
    # twice.
    _an_upgraded_edit(repo)
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    copy = (repo / cs.EDIT_HISTORY_FILENAME).read_bytes()
    prepare_state_dir(repo)
    added = load_history(repo)
    (repo / cs.EDIT_HISTORY_FILENAME).write_bytes(copy)

    prepare_state_dir(repo)

    assert load_history(repo) == added
    assert _state_in_tree(repo) == [cs.EDIT_LOCK_FILENAME]


def test_a_history_git_puts_back_does_not_bring_back_an_undone_edit(
    repo: Path,
) -> None:
    # Negative: a history committed by accident and restored by `git` shares
    # its entries with the one under CGR_HOME; it is a stale copy, dropped as
    # before, not a record of new edits.
    for text in ("return 2", "return 3"):
        tx = EditTransaction(repo)
        tx.stage("pkg/util.py", f"def helper():\n    {text}\n")
        assert tx.commit().applied
    committed = state_dir(repo) / cs.EDIT_HISTORY_FILENAME
    restored = committed.read_bytes()
    kept = _ids(repo)[:1]
    undo_last(repo)
    (repo / cs.EDIT_HISTORY_FILENAME).write_bytes(restored)

    prepare_state_dir(repo)

    assert _ids(repo) == kept
    assert _state_in_tree(repo) == []


def test_a_sync_never_waits_for_an_edit_in_progress(repo: Path) -> None:
    # Negative: the history is moved only with the edit lock, but a sync
    # that finds it taken goes on without moving it rather than waiting.
    tx = EditTransaction(repo)
    tx.stage("pkg/core.py", "def run():\n    return 0\n")
    assert tx.commit().applied
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    lock = state_dir(repo) / cs.EDIT_LOCK_FILENAME
    with lock.open("ab") as handle:
        _lock_file(handle)
        try:
            sync = threading.Thread(target=prepare_state_dir, args=(repo,))
            sync.start()
            assert _finishes(sync)
            assert (repo / cs.EDIT_HISTORY_FILENAME).is_file()
        finally:
            _unlock_file(handle)

    prepare_state_dir(repo)

    assert _ids(repo) == [tx.transaction_id, "older-cgr"]


def test_adding_an_older_cgrs_history_creates_nothing_in_the_tree(
    repo: Path,
) -> None:
    # Negative: only the lock the older cgr created stays in the tree.
    tx = EditTransaction(repo)
    tx.stage("pkg/core.py", "def run():\n    return 0\n")
    assert tx.commit().applied
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")

    prepare_state_dir(repo)
    undo_last(repo)

    assert _state_in_tree(repo) == [cs.EDIT_LOCK_FILENAME]


def test_an_older_cgrs_history_that_cannot_be_added_stays_in_the_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: CGR_HOME refuses the write. The sync goes on, and the tree's
    # history, the only record of the older cgr's edit, is kept for the next.
    tx = EditTransaction(repo)
    tx.stage("pkg/core.py", "def run():\n    return 0\n")
    assert tx.commit().applied
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    left = (repo / cs.EDIT_HISTORY_FILENAME).read_bytes()
    target = state_dir(repo) / cs.EDIT_HISTORY_FILENAME
    _replace_fails_for_target(target, monkeypatch)

    warnings = _warnings_during(lambda: prepare_state_dir(repo))

    assert (repo / cs.EDIT_HISTORY_FILENAME).read_bytes() == left
    pending = ls.CHECKOUT_STATE_HISTORY_PENDING.split("{", 1)[0]
    assert any(w.startswith(pending) for w in warnings), warnings


def _replace_fails_for_target(target: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace

    def replace(src: str | Path, dst: str | Path) -> None:
        if Path(dst) == target:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_replace(src, dst)

    monkeypatch.setattr("codebase_rag.checkout_state.os.replace", replace)


def test_an_unwritable_cgr_home_leaves_an_older_cgrs_history_in_the_tree(
    repo: Path,
    tmp_path: Path,
    mock_ingestor: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: no state directory, so the history stays where it is and
    # the sync still runs.
    _an_older_cgr_edits(repo, "pkg/util.py", _UTIL_BY_AN_OLDER_CGR, "older-cgr")
    left = (repo / cs.EDIT_HISTORY_FILENAME).read_bytes()
    blocked = tmp_path / "home-is-a-file"
    blocked.write_text("")
    monkeypatch.setattr(settings, "CGR_HOME", blocked)

    _sync(repo, mock_ingestor)

    assert (repo / cs.EDIT_HISTORY_FILENAME).read_bytes() == left
    assert _state_in_tree(repo) == sorted(
        [cs.EDIT_HISTORY_FILENAME, cs.EDIT_LOCK_FILENAME]
    )


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
