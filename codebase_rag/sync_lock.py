"""One sync of a checkout at a time (issue #2441).

Two writers syncing the same checkout at once (two terminals, the CLI and the
MCP server, the watcher and a manual `--update-graph`, two CI steps on one
runner) interleave their deletes and re-creates. One died on Memgraph's
TransientError; the other finished "successfully" over a graph missing a
third of its modules, then published a hash cache that made every later sync
report "already in sync", so the loss was permanent until `--clean`.

The lock is an OS advisory lock on `.cgr-sync-lock` beside the checkout's
other state files, the same kind the edit transactions take: the kernel
releases it however its holder dies, so a crashed sync never leaves a stale
lock to clear. Keyed by checkout rather than by project because the state
files it protects (the hash cache above all) are per checkout, and only the
holder can publish them, so no run publishes a cache while another writer
was changing the graph under it.

Re-entrant per thread: the CLI and the MCP tools take it before they put
the `:IncompleteRun` marker down, then call `GraphUpdater.run`, which takes
it again. Another thread of the same process is refused like another process
is, since two MCP registries in one server hold separate ingestor locks.

The lock file sits in a checkout that may come from anyone, so it is never
opened through a symbolic link: a planted `.cgr-sync-lock -> ~/.bashrc`
would otherwise have its target truncated and overwritten with the holder's
name. Such a link refuses the sync instead of falling back to an unguarded
one, and so does a lock file this user cannot open: a shared checkout's
second user would otherwise sync beside the first (review of PR 2512).
"""

from __future__ import annotations

import functools
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Concatenate, Protocol

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls

if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
    import msvcrt
else:
    import fcntl


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
    fd: int


_HELD: dict[str, _Hold] = {}
_HELD_GUARD = threading.Lock()

# O_NOFOLLOW refuses a symlink in the same call that opens the file, so a
# link swapped in after the `is_symlink` check still cannot redirect the
# write. Windows has no such flag; there the `is_symlink` check stands alone.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


def _try_lock(fd: int) -> bool:
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
    except OSError:
        return False
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


def _holder(lock_path: Path) -> str:
    try:
        fd = os.open(lock_path, os.O_RDONLY | _BINARY | _NO_FOLLOW)
        try:
            raw = os.read(fd, cs.SYNC_LOCK_HOLDER_MAX_BYTES)
        finally:
            os.close(fd)
        pid, project = raw.decode(cs.ENCODING_UTF8).splitlines()[:2]
        return ex.SYNC_HOLDER.format(pid=int(pid), project=project)
    except (OSError, ValueError):
        return ex.SYNC_HOLDER_UNKNOWN


def _refuse_link(lock_path: Path) -> UnsafeSyncLockError:
    return UnsafeSyncLockError(ex.SYNC_LOCK_IS_LINK.format(path=lock_path))


def _try_acquire(key: str, lock_path: Path, project_name: str) -> bool:
    with _HELD_GUARD:
        if key in _HELD:
            return False
        if lock_path.is_symlink():
            raise _refuse_link(lock_path)
        try:
            fd = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | _BINARY | _NO_FOLLOW, 0o644
            )
        except OSError as exc:
            # A link that appeared after the check above is refused, never
            # synced around: it is the planted file this guards against.
            if lock_path.is_symlink():
                raise _refuse_link(lock_path) from exc
            # Refused too: without the lock this run cannot tell whether
            # another one is syncing the checkout.
            raise SyncLockUnavailableError(
                ex.SYNC_LOCK_UNAVAILABLE.format(path=lock_path, error=exc)
            ) from exc
        if not _try_lock(fd):
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
) -> Iterator[None]:
    """Hold the sync lock of the checkout at `repo_path` for the block.

    Raises `SyncInProgressError`, naming the holder, when another writer
    holds it, unless `wait` is set: a scoped reingest (the watcher, the MCP
    reingest after an edit, `cgr check`) queues behind a running sync and
    applies its change to the finished graph instead of dropping it.

    Raises `UnsafeSyncLockError`, waiting or not, when the lock path is a
    symbolic link, and `SyncLockUnavailableError` when the lock file cannot
    be opened.
    """
    key = str(repo_path.resolve())
    if not _reenter(key):
        lock_path = repo_path / cs.SYNC_LOCK_FILENAME
        announced = False
        while not _try_acquire(key, lock_path, project_name):
            holder = _holder(lock_path)
            if not wait:
                raise SyncInProgressError(
                    ex.SYNC_IN_PROGRESS.format(repo=repo_path, holder=holder)
                )
            if not announced:
                logger.info(ls.SYNC_LOCK_WAITING, repo=repo_path, holder=holder)
                announced = True
            time.sleep(cs.SYNC_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        _release(key)


def holds_sync_lock[S: _Syncs, **P, R](
    *, wait: bool = False, require: Callable[[S], None] | None = None
) -> Callable[[Callable[Concatenate[S, P], R]], Callable[Concatenate[S, P], R]]:
    """Run the decorated method under its instance's checkout sync lock.

    A decorator rather than a `with` block inside the method, so the body
    and every phase call in it stay in the method itself: the phase order of
    `GraphUpdater.run` is pinned over the AST of `run`
    (`test_graph_updater_phase_order.py`).

    `require`, when given, runs before the lock is opened and raises when
    there is no checkout to sync: `GraphUpdater.run`'s missing-root check
    (issue #1651) must name the missing path, not the lock's failure to
    create its file inside it.
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
