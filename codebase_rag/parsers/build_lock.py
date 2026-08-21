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


_LEGACY_STALE_SECONDS = 600.0


_HOLDER_ALIVE = "alive"
_HOLDER_DEAD = "dead"
_HOLDER_UNKNOWN = "unknown"


def _legacy_holder_pid(lock: Path) -> int | None:
    try:
        pid = int((lock / "pid").read_text().strip())
    except (OSError, ValueError):
        return None
    # 0 probes the caller's own process group (always alive) and a negative
    # value targets a group, so neither identifies a holder.
    return pid if pid > 0 else None


def _legacy_holder_state(lock: Path) -> str:
    pid = _legacy_holder_pid(lock)
    if pid is None or os.name != "posix":
        return _HOLDER_UNKNOWN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return _HOLDER_DEAD
    except OverflowError:
        return _HOLDER_UNKNOWN
    except OSError:
        return _HOLDER_ALIVE
    return _HOLDER_ALIVE


def _legacy_dir_expired(lock: Path) -> bool:
    try:
        return time.time() - lock.stat().st_mtime > _LEGACY_STALE_SECONDS
    except OSError:
        return False


def _clear_stale_legacy_lock_dir(lock: Path) -> None:
    # Pre-#1227 versions used a mkdir lock at this same path; a crashed
    # holder's leftover directory would otherwise block the lock file from
    # ever being created. During a mixed-version upgrade window that
    # directory can belong to a LIVE old-version builder, so it is removed
    # only when its holder is provably dead (POSIX pid liveness) or the
    # directory has outlived any plausible build; otherwise the acquire loop
    # just keeps polling.
    if not lock.is_dir():
        return None
    state = _legacy_holder_state(lock)
    if state == _HOLDER_ALIVE:
        return None
    if state == _HOLDER_UNKNOWN and not _legacy_dir_expired(lock):
        return None
    try:
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        return None


def _open_lock_file(lock: Path) -> int | None:
    try:
        return os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
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
    fd: int | None = None
    try:
        for _ in range(tries):
            if fd is None:
                _clear_stale_legacy_lock_dir(lock)
                fd = _open_lock_file(lock)
            if fd is not None and _try_lock(fd):
                handle, fd = BuildLock(fd), None
                return handle
            time.sleep(poll_seconds)
            if artifact_fresh():
                return None
        return None
    finally:
        if fd is not None:
            os.close(fd)


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
