"""Crash-safe mkdir build lock shared by the semantic frontends (issue #1227).

The Go and C# frontends serialise their one tool build across parallel
workers with a mkdir lock. A holder killed between mkdir and the releasing
rmdir used to leave the lock behind forever: every later run waited the full
retry budget and returned empty facts until the directory was removed by
hand. The lock now records its owner's pid; a waiter reclaims the lock when
that owner is demonstrably dead (POSIX pid liveness) or, where liveness
cannot be probed safely, when the lock has outlived any plausible build
(mtime age). Reclamation is best-effort and race-tolerant: losing a reclaim
race just means another waiter got the lock first.
"""

import os
import time
from collections.abc import Callable
from pathlib import Path

_LOCK_PID_FILE = "pid"
_LOCK_STALE_SECONDS = 600.0
_OS_POSIX = "posix"


def _holder_pid(lock: Path) -> int | None:
    try:
        return int((lock / _LOCK_PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def _lock_is_stale(lock: Path) -> bool:
    pid = _holder_pid(lock)
    if pid is not None and os.name == _OS_POSIX:
        # os.kill(pid, 0) probes liveness without signalling on POSIX; on
        # other platforms it can terminate the target, so those fall through
        # to the age check.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        return False
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    return age > _LOCK_STALE_SECONDS


def _reclaim(lock: Path) -> None:
    try:
        (lock / _LOCK_PID_FILE).unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        return None


def acquire_build_lock(
    lock: Path,
    artifact_fresh: Callable[[], bool],
    tries: int,
    poll_seconds: float,
) -> bool:
    """Take the mkdir lock, reclaiming it from a dead holder.

    Returns True holding the lock (caller must release_build_lock); False when
    another worker already produced a fresh artifact or the tries ran out.
    """
    for _ in range(tries):
        try:
            lock.mkdir()
        except FileExistsError:
            if _lock_is_stale(lock):
                _reclaim(lock)
                continue
            time.sleep(poll_seconds)
            if artifact_fresh():
                return False
            continue
        try:
            (lock / _LOCK_PID_FILE).write_text(str(os.getpid()))
        except OSError:
            # An unwritable pid file only disables liveness-based
            # reclamation; the age fallback still applies.
            return True
        return True
    return False


def release_build_lock(lock: Path) -> None:
    _reclaim(lock)
