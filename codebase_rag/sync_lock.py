"""One sync of a checkout at a time (issue #2441).

Two writers syncing the same checkout at once (two terminals, the CLI and the
MCP server, the watcher and a manual `--update-graph`, two CI steps on one
runner) interleave their deletes and re-creates. One died on Memgraph's
TransientError; the other finished "successfully" over a graph missing a
third of its modules, then published a hash cache that made every later sync
report "already in sync", so the loss was permanent until `--clean`.

The lock is an OS advisory lock on `.cgr-sync-lock` in the checkout's state
directory under CGR_HOME, beside the hash cache and the edit lock (issue
#2427), and the same kind the edit transactions take: the kernel releases it
however its holder dies, so a crashed sync never leaves a stale lock to
clear. Keyed by checkout rather than by project because the state files it
protects (the hash cache above all) are per checkout, and only the holder
can publish them, so no run publishes a cache while another writer was
changing the graph under it. Like the rest of that state it is per user:
users with separate CGR_HOMEs sync one checkout under separate locks.

Windows releases a dead process's lock asynchronously, though, so a sync
rerun right after one was killed can find the lock still held, in the name
of a process that no longer exists. A run that would be refused retries
for a short grace while the holder the lock file names has exited; a
holder that is running, or that the file does not name, is refused at
once.

Nothing is written into the tree, and a `.cgr-sync-lock` found there is
never opened, moved or removed: it is in `CGR_STATE_LOCK_FILENAMES`, so the
state move a sync makes under the lock leaves it where it is instead of
carrying a held lock file away from the path the next writer opens.

A lock file that is there but will not open read-write, on a read-only
CGR_HOME or as another user's file in a CGR_HOME they share, is locked
through a read-only handle: the lock call needs no write access, so it
still excludes. Only a lock file that is not there and cannot be created,
because CGR_HOME cannot be written, lets a sync run without the lock: such
a sync runs without incremental state rather than failing (issue #2427), so
it runs without the lock too, after a warning that concurrent syncs of the
checkout are not excluded. It cannot publish a hash cache either, since
that lives in the same directory.

Re-entrant per thread: the CLI and the MCP tools take it before they put
the `:IncompleteRun` marker down, then call `GraphUpdater.run`, which takes
it again. Another thread of the same process is refused like another process
is, since two MCP registries in one server hold separate ingestor locks.

The lock file is never opened through a symbolic link: a link planted at
its path would otherwise have its target truncated and overwritten with the
holder's name. Such a link refuses the sync instead of falling back to an
unguarded one, and so does a lock file that is there but this user cannot
open even read-only: the second user of a shared CGR_HOME would otherwise
sync beside the first (review of PR 2512).
"""

from __future__ import annotations

import errno
import functools
import os
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Concatenate, Protocol

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .checkout_state import prepare_state_dir
from .utils.process_owner import pid_has_exited

if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
    import msvcrt

    # What the non-blocking lock call raises while another holder has the
    # lock: the CRT's `_locking` maps LK_NBLCK's lock violation to EACCES.
    # (EDEADLOCK is its error for the blocking modes, never used here.)
    _CONTENDED = frozenset({errno.EACCES})
else:
    import fcntl

    # flock(2) with LOCK_NB: EWOULDBLOCK, which is EAGAIN on Linux.
    _CONTENDED = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})


class SyncLockError(RuntimeError):
    """The checkout's sync lock was not taken, so the sync did not start."""


class SyncInProgressError(SyncLockError):
    """Another process, or another thread of this one, is syncing the checkout."""


class UnsafeSyncLockError(SyncLockError):
    """The lock path is a symbolic link, which a sync never writes through."""


class SyncLockUnavailableError(SyncLockError):
    """The lock file could not be opened, so no lock could be taken."""


class _Syncs(Protocol):
    """What `holds_sync_lock` reads off the instance whose method it wraps."""

    @property
    def repo_path(self) -> Path: ...

    @property
    def project_name(self) -> str: ...


@dataclass
class _Hold:
    thread: int
    depth: int
    # None while the sync runs without the lock file (CGR_HOME unwritable).
    fd: int | None


_HELD: dict[str, _Hold] = {}
_HELD_GUARD = threading.Lock()

# O_NOFOLLOW refuses a symlink in the same call that opens the file, so a
# link swapped in after the `is_symlink` check still cannot redirect the
# write. Windows has no such flag; there the `is_symlink` check stands alone.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)

# What creating the lock file fails with when its directory cannot be
# written or does not exist: CGR_HOME is read-only or not a directory, so
# the state directory could not be made (issue #2427).
_UNWRITABLE = frozenset(
    {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT, errno.ENOTDIR}
)
# What a read-write open of a lock file that is there fails with while this
# user may still read it: a read-only CGR_HOME (EROFS), or a file another
# user created 0644 in a CGR_HOME they share (EACCES, EPERM). The lock call
# needs no write access (flock(2); Windows' LockFile takes a handle opened
# for reading), so such a file is locked through a read-only handle.
_READ_ONLY_OPEN = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})


def _try_lock(fd: int) -> bool:
    """Lock `fd` without blocking; False only while another holder has it.

    Any other failure raises: a lock call that cannot work (ENOLCK, a
    filesystem without locks) read as contention refused a sync nobody
    was running, and a waiting reingest polled for it forever (review of
    PR 2512).
    """
    try:
        if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
            # A Windows byte-range lock also stops other processes READING
            # the locked bytes, so it locks a byte past the holder's name
            # rather than over it: a refused writer must still read whom it
            # is waiting for.
            os.lseek(fd, cs.SYNC_LOCK_REGION_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _CONTENDED:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
        os.lseek(fd, cs.SYNC_LOCK_REGION_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _record_holder(fd: int, project_name: str) -> None:
    # Best effort: the lock is what excludes, this only names the holder in
    # the refusal a second writer prints. Truncated last, so a failed
    # truncation still leaves this holder's two lines first in the file,
    # and only the first two lines are read.
    holder = f"{os.getpid()}\n{project_name}\n".encode(cs.ENCODING_UTF8)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, holder)
        os.ftruncate(fd, len(holder))
    except OSError:
        return


@dataclass(frozen=True)
class _HolderRecord:
    pid: int
    project: str


def _holder(lock_path: Path) -> _HolderRecord | None:
    """The holder the lock file names, or None when it names none readably."""
    try:
        fd = os.open(lock_path, os.O_RDONLY | _BINARY | _NO_FOLLOW)
        try:
            raw = os.read(fd, cs.SYNC_LOCK_HOLDER_MAX_BYTES)
        finally:
            os.close(fd)
        pid, project = raw.decode(cs.ENCODING_UTF8).splitlines()[:2]
        return _HolderRecord(int(pid), project)
    except (OSError, ValueError):
        return None


def _describe(holder: _HolderRecord | None) -> str:
    if holder is None:
        return ex.SYNC_HOLDER_UNKNOWN
    return ex.SYNC_HOLDER.format(pid=holder.pid, project=holder.project)


def _cannot_create(lock_path: Path, error: OSError) -> bool:
    """True when there is no lock file and `error` says none can be created.

    That is CGR_HOME that cannot be written, the one case a sync runs
    without the lock. A lock file that is there is never synced around,
    whatever its open fails with: another sync may hold it.
    """
    return error.errno in _UNWRITABLE and not os.path.lexists(lock_path)


def _refuse_link(lock_path: Path) -> UnsafeSyncLockError:
    return UnsafeSyncLockError(ex.SYNC_LOCK_IS_LINK.format(path=lock_path))


def _unavailable(lock_path: Path, error: OSError) -> SyncLockUnavailableError:
    return SyncLockUnavailableError(
        ex.SYNC_LOCK_UNAVAILABLE.format(path=lock_path, error=error)
    )


def _open_lock(lock_path: Path) -> int | OSError:
    """A descriptor on the lock file, or why there is none and can be none.

    The error comes back only when the file is not there and cannot be
    created (`_cannot_create`). Raises `UnsafeSyncLockError` for a link and
    `SyncLockUnavailableError` for a lock file that is there but will not
    open even read-only: without the lock this run cannot tell whether
    another one is syncing the checkout.
    """
    try:
        return os.open(lock_path, os.O_RDWR | os.O_CREAT | _BINARY | _NO_FOLLOW, 0o644)
    except OSError as exc:
        # A link that appeared after the caller's check is refused, never
        # synced around: it is the planted file this guards against.
        if lock_path.is_symlink():
            raise _refuse_link(lock_path) from exc
        if _cannot_create(lock_path, exc):
            return exc
        if exc.errno not in _READ_ONLY_OPEN:
            raise _unavailable(lock_path, exc) from exc
    # Greptile review of PR 2512: a lock file that would not open read-write
    # on a read-only CGR_HOME was taken for one with no lock, so the sync ran
    # beside the one holding it. Locked read-only it still excludes; the
    # holder's name is not recorded then, so a refusal may name an earlier
    # holder.
    try:
        return os.open(lock_path, os.O_RDONLY | _BINARY | _NO_FOLLOW)
    except OSError as exc:
        if lock_path.is_symlink():
            raise _refuse_link(lock_path) from exc
        raise _unavailable(lock_path, exc) from exc


def _try_acquire(key: str, repo_path: Path, lock_path: Path, project_name: str) -> bool:
    with _HELD_GUARD:
        if key in _HELD:
            return False
        if lock_path.is_symlink():
            raise _refuse_link(lock_path)
        fd = _open_lock(lock_path)
        if isinstance(fd, OSError):
            # CGR_HOME cannot be written, which never fails a sync (issue
            # #2427): run without the lock and say what that risks. Still
            # recorded as held, so the run re-enters it and this process's
            # other threads are still refused.
            logger.warning(
                ls.SYNC_LOCK_UNGUARDED, path=lock_path, error=fd, repo=repo_path
            )
            _HELD[key] = _Hold(threading.get_ident(), 1, None)
            return True
        try:
            locked = _try_lock(fd)
        except OSError as exc:
            # The lock call itself failed; no holder refused it, so neither
            # "another sync is running" nor waiting for one would be true.
            os.close(fd)
            raise _unavailable(lock_path, exc) from exc
        if not locked:
            os.close(fd)
            return False
        _record_holder(fd, project_name)
        _HELD[key] = _Hold(threading.get_ident(), 1, fd)
        return True


def _release(key: str) -> None:
    with _HELD_GUARD:
        hold = _HELD[key]
        hold.depth -= 1
        if hold.depth:
            return
        del _HELD[key]
    if hold.fd is None:
        return
    try:
        _unlock(hold.fd)
    finally:
        os.close(hold.fd)


def _reenter(key: str) -> bool:
    with _HELD_GUARD:
        hold = _HELD.get(key)
        if hold is None or hold.thread != threading.get_ident():
            return False
        hold.depth += 1
        return True


@contextmanager
def repo_sync_lock(
    repo_path: Path, project_name: str, *, wait: bool = False
) -> Generator[None]:
    """Hold the sync lock of the checkout at `repo_path` for the block.

    Raises `SyncInProgressError`, naming the holder, when another writer
    holds it, unless `wait` is set: a scoped reingest (the watcher, the MCP
    reingest after an edit, `cgr check`) queues behind a running sync and
    applies its change to the finished graph instead of dropping it. A
    holder the lock file names that has exited is not refused at once: its
    lock gets `SYNC_LOCK_RELEASE_GRACE_SECONDS` to be released, as Windows
    does some time after the process dies.

    The lock file is `.cgr-sync-lock` in the checkout's state directory
    under CGR_HOME; one this user can only read is locked read-only. When
    there is none and none can be created because CGR_HOME cannot be
    written, the block runs without it after a `SYNC_LOCK_UNGUARDED`
    warning, as the rest of the sync runs without its state then.

    Raises `UnsafeSyncLockError`, waiting or not, when the lock path is a
    symbolic link, and `SyncLockUnavailableError` when a lock file that is
    there cannot be opened even read-only or the lock call fails for any
    reason but another holder.
    """
    key = str(repo_path.resolve())
    if not _reenter(key):
        # Created, not adopted: the sync moves what an older cgr left in the
        # tree once it holds the lock, as the edit lock does (issue #2427).
        lock_path = prepare_state_dir(repo_path, adopt=False) / cs.SYNC_LOCK_FILENAME
        _acquire(key, repo_path, lock_path, project_name, wait=wait)
    try:
        yield
    finally:
        _release(key)


def _release_deadline(
    holder: _HolderRecord | None, deadline: float | None
) -> float | None:
    # A holder that has exited no longer syncs; its lock is only waiting for
    # Windows to release it. None refuses the lock now: a live or unknown
    # holder at once, without reading the clock, and a dead one's once the
    # grace has run out.
    if holder is None or not pid_has_exited(holder.pid):
        return None
    now = time.monotonic()
    if deadline is None:
        deadline = now + cs.SYNC_LOCK_RELEASE_GRACE_SECONDS
    return deadline if now < deadline else None


def _acquire(
    key: str, repo_path: Path, lock_path: Path, project_name: str, *, wait: bool
) -> None:
    announced = False
    release_deadline: float | None = None
    while not _try_acquire(key, repo_path, lock_path, project_name):
        # Read again on every try, so a holder that takes the lock meanwhile
        # is refused at once.
        holder = _holder(lock_path)
        if not wait:
            release_deadline = _release_deadline(holder, release_deadline)
            if release_deadline is None:
                raise SyncInProgressError(
                    ex.SYNC_IN_PROGRESS.format(repo=repo_path, holder=_describe(holder))
                )
            time.sleep(cs.SYNC_LOCK_RELEASE_POLL_SECONDS)
            continue
        if not announced:
            logger.info(ls.SYNC_LOCK_WAITING, repo=repo_path, holder=_describe(holder))
            announced = True
        time.sleep(cs.SYNC_LOCK_POLL_SECONDS)


def holds_sync_lock[S: _Syncs, **P, R](
    *, wait: bool = False, require: Callable[[S], None] | None = None
) -> Callable[[Callable[Concatenate[S, P], R]], Callable[Concatenate[S, P], R]]:
    """Run the decorated method under its instance's checkout sync lock.

    A decorator rather than a `with` block inside the method, so the body
    and every phase call in it stay in the method itself: the phase order of
    `GraphUpdater.run` is pinned over the AST of `run`
    (`test_graph_updater_phase_order.py`).

    `require`, when given, runs before the lock is taken and raises when
    there is no checkout to sync: `GraphUpdater.run`'s missing-root check
    (issue #1651) names the missing path before a state directory is made
    for a checkout that is not there.
    """

    def decorate(
        method: Callable[Concatenate[S, P], R],
    ) -> Callable[Concatenate[S, P], R]:
        @functools.wraps(method)
        def locked(self: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
            if require is not None:
                require(self)
            with repo_sync_lock(self.repo_path, self.project_name, wait=wait):
                return method(self, *args, **kwargs)

        return locked

    return decorate
