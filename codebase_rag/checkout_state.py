"""Where cgr keeps what it knows about one checkout (issue #2427).

Every sync wrote its incremental state (hash cache, directory mtimes,
exclusion stamp, parser fingerprint, pending markers), and every edit its
history and lock, into the root of the indexed checkout. A clean `git status`
became a list of untracked `.cgr-*` files, a routine `git add -A` committed
machine-local state (mtimes, a project name hashed from the clone's absolute
path), a committed state file then showed as modified after every sync and in
`cgr check`, and a read-only checkout could not be synced incrementally.

The state lives in one directory per checkout instead,
`CGR_HOME/state/<name>__<hash>`, named the way `derive_project_name` names
the checkout's default project. It is keyed by the checkout rather than by
the project because the state belongs to the tree: a checkout indexed under
several `--project-name`s shares one hash cache, and the exclusion stamp
keeps each project's entry inside that one file.

Nothing here is specific to one state file. Every name in
`CGR_STATE_FILENAMES` that an older cgr left in the checkout root is moved
into the directory the first time the checkout's state is reached, so an
upgrade keeps its hash cache (and the mtime the in-sync check compares
files against) and its edit history, and a name added to the set is kept
out of the tree and migrated the same way. A lock is the exception
(`CGR_STATE_LOCK_FILENAMES`): it stays in the tree, where an older cgr
still running takes it, and an edit takes it as well as its own
(`EditLock`).

The edit history is the other exception. An older cgr still running goes
on recording its edits there, starting a new history once the old one has
moved. That history is moved, or its entries are added to the one under
CGR_HOME, only while both edit locks are held, and whenever they are next
held, so an entry it records later is never dropped (Greptile, PRs #2490
and #2550).

A move never replaces a file already in the directory. Another process
may publish the checkout's current state there at any moment, and the
tree's copy is older than anything it publishes, so the move copies the
file beside its destination and then links the copy into place, which
fails rather than replaces when the destination exists. The copy keeps
the mtime, and the destination never shares an inode with the tree's
copy, which state written in place would otherwise change too.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from pathlib import Path
from types import TracebackType
from typing import IO

from loguru import logger

from . import constants as cs
from . import logs as ls
from .config import settings
from .utils.path_utils import derive_project_name


def state_dir(repo_path: Path) -> Path:
    """The directory holding the state of the checkout at `repo_path`.

    Nothing is created or moved, so a lookup (has cgr synced this
    ancestor?) leaves no directory behind.
    """
    return (
        settings.CGR_HOME.expanduser()
        / cs.CGR_STATE_DIRNAME
        / derive_project_name(repo_path)
    )


def prepare_state_dir(repo_path: Path, *, adopt: bool = True) -> Path:
    """`state_dir(repo_path)`, created, with the state left in the tree moved in.

    Creating it is best effort. When CGR_HOME cannot be written the problem
    is logged and the directory returned anyway: reads find nothing and
    writes fail the way they failed on a read-only tree, so the sync runs
    without incremental state instead of stopping.

    `adopt=False` only creates it, for the edit lock.
    """
    directory = state_dir(repo_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logger.warning(ls.CHECKOUT_STATE_DIR_UNAVAILABLE, path=directory, error=error)
        return directory
    if not adopt:
        return directory
    adopted = [
        name
        for name in sorted(
            cs.CGR_STATE_FILENAMES
            - cs.CGR_STATE_LOCK_FILENAMES
            - {cs.EDIT_HISTORY_FILENAME}
        )
        if _adopt(repo_path / name, directory / name)
    ]
    if adopted:
        logger.info(
            ls.CHECKOUT_STATE_ADOPTED,
            repo=repo_path,
            path=directory,
            names=", ".join(adopted),
        )
    if _is_regular(repo_path / cs.EDIT_HISTORY_FILENAME):
        _adopt_history_unless_editing(repo_path)
    return directory


def state_file(repo_path: Path, name: str) -> Path:
    """Where the checkout's state file `name` is read and written."""
    return prepare_state_dir(repo_path) / name


def _adopt(legacy: Path, target: Path) -> bool:
    """Move `legacy`, left in the tree by an older cgr, to `target`.

    Returns whether it became `target`. A `target` that exists, or appears
    while the move runs, is kept and `legacy` dropped.
    """
    if not _is_regular(legacy):
        return False
    try:
        placed = not target.exists() and _place(legacy, target)
    except FileExistsError:
        placed = False
    except OSError as error:
        # The tree's copy is all there is, so it stays.
        logger.warning(
            ls.CHECKOUT_STATE_ADOPT_FAILED, legacy=legacy, target=target, error=error
        )
        return False
    # Moved, or the directory holds the copy kept current since the upgrade
    # (or one a sync just published). A copy in the tree is older, for
    # instance restored by checking out a commit that still tracked it, and
    # taking it would roll the state back; either way it goes.
    _discard(legacy)
    return placed


def _is_regular(legacy: Path) -> bool:
    # Only a regular file is cgr's own. A link under a state file's name,
    # say in a cloned repository, would point cgr's later writes wherever
    # it leads, so it is neither followed nor moved.
    try:
        return stat.S_ISREG(legacy.lstat().st_mode)
    except OSError:
        return False


def _place(legacy: Path, target: Path) -> bool:
    """Copy `legacy` to `target` unless `target` exists, keeping its mtime.

    Raises FileExistsError when `target` appeared meanwhile. Returns False
    when `legacy` is gone: another process moved it first.
    """
    handle, name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=cs.TMP_EXTENSION
    )
    os.close(handle)
    temp = Path(name)
    try:
        # copy2 keeps the mtime, which the in-sync check compares every
        # cached file against.
        shutil.copy2(legacy, temp)
        _link_no_replace(temp, target)
    except FileNotFoundError:
        if legacy.exists():
            raise
        return False
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
    return True


def _link_no_replace(source: Path, target: Path) -> None:
    """Give `source` the name `target`, raising FileExistsError if it is taken.

    A hard link is that operation on POSIX and on NTFS alike, and atomic:
    the destination is never seen half written. `os.replace` replaces on
    every platform, and `os.rename` replaces on POSIX, so neither will do.
    """
    try:
        os.link(source, target)
    except (FileExistsError, FileNotFoundError):
        raise
    except OSError:
        # A filesystem without hard links (FAT, some network shares).
        _create_exclusive(source, target)


def _create_exclusive(source: Path, target: Path) -> None:
    """Write `source` to a new `target`, failing if one exists (O_EXCL).

    Never replaces, like the link, but is not atomic: a reader in the
    instant it takes sees a short file, read as no state. Only a CGR_HOME
    without hard links comes here.
    """
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(target, flags, cs.EDIT_TEMP_FILE_MODE)
    # The file this call created, read back by name at once: on Windows no
    # other process can replace a file this one holds open, and on POSIX a
    # replace would have to land between these two calls.
    created = target.lstat()
    try:
        with os.fdopen(descriptor, "wb") as out, source.open("rb") as data:
            shutil.copyfileobj(data, out)
        shutil.copystat(source, target)
    except OSError:
        # Half written, or without the mtime the in-sync check relies on: it
        # must not pass for the state, or the next run drops the tree's copy
        # as stale.
        _unlink_if_created(target, created)
        raise


def _unlink_if_created(target: Path, created: os.stat_result) -> None:
    """Remove `target` only while it is still the file `created` describes.

    An edit publishes its history with `os.replace` under the edit lock,
    which a sync moving the tree's copy does not hold. A file it published
    while the copy ran is the newest state there is, and the failed copy's
    cleanup must leave it (Greptile, PR #2557).
    """
    with contextlib.suppress(OSError):
        if os.path.samestat(created, target.lstat()):
            target.unlink()


def _discard(legacy: Path) -> None:
    try:
        legacy.unlink(missing_ok=True)
    except OSError as error:
        logger.debug(ls.CHECKOUT_STATE_LEGACY_LEFT, legacy=legacy, error=error)


# --- the edit history an older cgr records in the tree ------------------------


def _adopt_history_unless_editing(repo_path: Path) -> None:
    """Move in the tree's edit history if no one is editing the checkout.

    Never waits: a sync must not stall behind an edit's verifier, and a
    verifier that reaches the state itself would wait on its own lock. An
    edit in progress, this cgr's or an older one's, leaves the history for
    whoever next takes the edit lock.
    """
    try:
        with EditLock(repo_path) as lock:
            lock.acquire(wait=False)
    except OSError as error:
        logger.warning(
            ls.CHECKOUT_STATE_ADOPT_FAILED,
            legacy=repo_path / cs.EDIT_HISTORY_FILENAME,
            target=state_dir(repo_path) / cs.EDIT_HISTORY_FILENAME,
            error=error,
        )


def _adopt_history(repo_path: Path, directory: Path) -> None:
    """Move the tree's edit history into `directory`, with the edit lock held.

    An older cgr that edits after the history moved records its entries in
    a new history at the root, so a history found there while the one
    under CGR_HOME exists is added to it rather than dropped.

    Only a history that shares no entry with the one under CGR_HOME is
    added. The older cgr started its history afresh once the old one
    moved, so one that shares an entry is an older copy, such as one `git`
    put back from a commit that tracked it: it is dropped as any stale
    copy is, since adding it would bring back the edits undone since.
    """
    legacy = repo_path / cs.EDIT_HISTORY_FILENAME
    target = directory / cs.EDIT_HISTORY_FILENAME
    if not _is_regular(legacy):
        return
    if not target.exists():
        if _adopt(legacy, target):
            logger.info(
                ls.CHECKOUT_STATE_ADOPTED,
                repo=repo_path,
                path=directory,
                names=cs.EDIT_HISTORY_FILENAME,
            )
        return
    try:
        recorded = _history_entries(legacy)
        kept = _history_entries(target)
        known = {entry.get(cs.EDIT_KEY_ID) for entry in kept}
        if recorded and known.isdisjoint(
            entry.get(cs.EDIT_KEY_ID) for entry in recorded
        ):
            _write_history(target, [*kept, *recorded])
            logger.info(
                ls.CHECKOUT_STATE_HISTORY_MERGED,
                count=len(recorded),
                legacy=legacy,
                target=target,
            )
    except OSError as error:
        # The tree's copy may hold the only record of an older cgr's edits.
        logger.warning(
            ls.CHECKOUT_STATE_ADOPT_FAILED, legacy=legacy, target=target, error=error
        )
        return
    _discard(legacy)


def _history_entries(path: Path) -> list[dict]:
    """The entries of the edit history at `path`; none if it is not one.

    Raises OSError when it cannot be read, so a history that may still
    hold entries is never taken for an empty one.
    """
    try:
        data = json.loads(path.read_text(encoding=cs.ENCODING_UTF8))
    except ValueError:
        return []
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        return []
    return data


def _write_history(target: Path, entries: list[dict]) -> None:
    handle, name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=cs.TMP_EXTENSION
    )
    temp = Path(name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(
                json.dumps(entries[-cs.EDIT_HISTORY_LIMIT :], indent=2).encode(
                    cs.ENCODING_UTF8
                )
            )
        os.replace(temp, target)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


# --- the edit lock ------------------------------------------------------------

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(directory: Path) -> threading.Lock:
    key = str(directory)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _lock_file(handle: IO[bytes]) -> None:
    if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _try_lock_file(handle: IO[bytes]) -> bool:
    """Lock `handle` only if no one holds it; returns whether it did."""
    if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_file(handle: IO[bytes]) -> None:
    if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_tree_lock(repo_path: Path) -> IO[bytes] | None:
    """The lock an older cgr takes at the checkout root, if one is there.

    Opened for reading, never created: a tree no older cgr edited gets no
    lock file. Only a regular file is cgr's; a link or a FIFO by that name
    is neither followed nor opened.
    """
    path = repo_path / cs.EDIT_LOCK_FILENAME
    if not _is_regular(path):
        return None
    try:
        return path.open("rb")
    except OSError:
        return None


class EditLock:
    """The lock every edit of the checkout at `repo_path` holds.

    Two transactions committing into one tree must serialise their
    read-verify-write windows, and `cgr edits undo` may run in another
    process than the agent's server: the lock is an OS advisory lock on
    `.cgr-edit-lock` in the state directory, held across the baseline
    check, the verifier, the writes and the history update. A thread lock
    per checkout sits in front of it so threads of one process queue rather
    than contend for the file.

    An older cgr locks `.cgr-edit-lock` at the checkout root instead, and
    one still running (an agent's server started before the upgrade) goes
    on doing so. That file is never created, moved or removed, since
    another process may hold it or be about to open it by that path; while
    it is there it is taken too, after ours, so old and new editors of one
    tree exclude each other. Each time both are taken, the history that
    older cgr records at the root is moved in (`_adopt_history`).
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path
        self._directory = state_dir(repo_path)
        self._held = contextlib.ExitStack()

    def __enter__(self) -> EditLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._held.close()

    def acquire(self, *, wait: bool = True) -> bool:
        """Take the lock; with `wait=False` only if no one holds any part.

        Returns whether it is held. What was taken is released on exit
        either way.
        """
        thread_lock = _thread_lock(self._directory)
        if not thread_lock.acquire(blocking=wait):
            return False
        self._held.callback(thread_lock.release)
        lock_path = prepare_state_dir(self._repo_path, adopt=False) / (
            cs.EDIT_LOCK_FILENAME
        )
        own = self._held.enter_context(lock_path.open("ab"))
        if not (self._take(own, wait=wait) and self._take_tree_lock(wait=wait)):
            return False
        _adopt_history(self._repo_path, self._directory)
        return True

    def _take_tree_lock(self, *, wait: bool) -> bool:
        """Take the older cgr's lock if it is there; False if it is busy."""
        handle = _open_tree_lock(self._repo_path)
        if handle is None:
            return True
        self._held.enter_context(handle)
        return self._take(handle, wait=wait)

    def _take(self, handle: IO[bytes], *, wait: bool) -> bool:
        if wait:
            _lock_file(handle)
        elif not _try_lock_file(handle):
            return False
        self._held.callback(_unlock_file, handle)
        return True
