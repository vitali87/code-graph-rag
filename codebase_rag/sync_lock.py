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
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls

if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
    import msvcrt
else:
    import fcntl


class SyncInProgressError(RuntimeError):
    """Another process, or another thread of this one, is syncing the checkout."""


@dataclass
class _Hold:
    thread: int
    depth: int
    fd: int | None


_HELD: dict[str, _Hold] = {}
_HELD_GUARD = threading.Lock()


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _record_holder(fd: int, project_name: str) -> None:
    # Best effort: the lock is what excludes, this only names the holder in
    # the refusal a second writer prints.
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n{project_name}\n".encode(cs.ENCODING_UTF8))
    except OSError:
        return


def _holder(lock_path: Path) -> str:
    try:
        pid, project = lock_path.read_text(encoding=cs.ENCODING_UTF8).splitlines()[:2]
        return ex.SYNC_HOLDER.format(pid=int(pid), project=project)
    except (OSError, ValueError):
        return ex.SYNC_HOLDER_UNKNOWN


def _try_acquire(key: str, lock_path: Path, project_name: str) -> bool:
    with _HELD_GUARD:
        if key in _HELD:
            return False
        try:
            fd = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644
            )
        except OSError as exc:
            # A read-only or unusual checkout syncs unguarded, as it did
            # before the lock existed, rather than not at all. Threads of
            # this process still exclude each other through `_HELD`.
            logger.debug(ls.SYNC_LOCK_UNAVAILABLE, path=lock_path, error=exc)
            _HELD[key] = _Hold(threading.get_ident(), 1, None)
            return True
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
    if hold.fd is not None:
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
