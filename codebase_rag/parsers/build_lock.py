"""Crash-safe build lock shared by the semantic frontends (issue #1227).

The Go and C# frontends serialise their one tool build across parallel
workers. A mkdir lock needs staleness heuristics (and those heuristics race:
a waiter that classified a lock stale can delete a lock another waiter
already reclaimed), so the lock is an OS-level file lock instead: flock on
POSIX, msvcrt.locking on Windows. The kernel releases either one when the
holding process dies, however it dies, so an abandoned lock cannot exist and
nothing ever needs reclaiming.
"""

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class BuildLock:
    """An acquired lock: an open descriptor holding the OS lock."""

    def __init__(self, fd: int) -> None:
        self.fd = fd


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _clear_legacy_lock_dir(lock: Path) -> None:
    # Pre-#1227 versions used a mkdir lock at this same path; a crashed
    # holder's leftover directory would otherwise block the lock file from
    # ever being created.
    if not lock.is_dir():
        return None
    try:
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        return None


def acquire_build_lock(
    lock: Path,
    artifact_fresh: Callable[[], bool],
    tries: int,
    poll_seconds: float,
) -> BuildLock | None:
    """Take the build lock, polling until it frees or the artifact appears.

    Returns the held lock (caller must release_build_lock), or None when
    another worker already produced a fresh artifact, the tries ran out, or
    the lock file cannot be opened at all.
    """
    _clear_legacy_lock_dir(lock)
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    for _ in range(tries):
        if _try_lock(fd):
            return BuildLock(fd)
        time.sleep(poll_seconds)
        if artifact_fresh():
            os.close(fd)
            return None
    os.close(fd)
    return None


def release_build_lock(handle: BuildLock | None) -> None:
    if handle is None:
        return None
    try:
        if sys.platform == "win32":
            os.lseek(handle.fd, 0, os.SEEK_SET)
            msvcrt.locking(handle.fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        os.close(handle.fd)
