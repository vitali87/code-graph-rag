from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from loguru import logger

from .config import settings

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

STATE_FILENAME = "state.json"


class _StateShape(TypedDict, total=False):
    last_sync: dict[str, str]


def state_path(home: Path | None = None) -> Path:
    base = (home or settings.CGR_HOME).expanduser()
    return base / STATE_FILENAME


def _load(path: Path) -> _StateShape:
    if not path.exists():
        return _StateShape()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _StateShape()
        last_sync = data.get("last_sync", {})
        if not isinstance(last_sync, dict):
            logger.warning(f"Ignoring malformed cgr state in {path}: last_sync")
            return _StateShape()
        return _StateShape(last_sync=last_sync)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load cgr state from {path}: {e}")
    return _StateShape()


def _save(path: Path, data: _StateShape) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.warning(f"Failed to save cgr state to {path}: {e}")


def _close_quietly(lock_file) -> None:
    try:
        lock_file.close()
    except OSError as e:
        logger.warning(f"State lock file could not be closed: {e}")


@contextmanager
def _locked(path: Path):
    """Serialize whole-file read-modify-writes on the state (#2479).

    Every writer rewrites the file from a read of it, so two concurrent
    writers can silently drop each other's update -- a sync finishing while
    a prune forgets the purged project would resurrect the ghost record.
    A lock that cannot be taken or released (an unwritable home, a
    filesystem without the lock call, an NFS close raising) degrades to the
    unlocked write with a warning: the state was never guaranteed against a
    broken home, and `record_sync` must not grow a new way to fail a sync
    whose graph commit already succeeded.
    """
    lock_path = path.parent / f"{path.name}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a")
    except OSError as e:
        logger.warning(f"State write proceeding without a lock: {e}")
        yield
        return
    try:
        if sys.platform == "win32":
            os.lseek(lock_file.fileno(), 0, os.SEEK_SET)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_RLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        _close_quietly(lock_file)
        logger.warning(f"State write proceeding without a lock: {e}")
        yield
        return
    try:
        yield
    finally:
        try:
            if sys.platform == "win32":
                os.lseek(lock_file.fileno(), 0, os.SEEK_SET)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as e:
            logger.warning(f"State lock could not be released: {e}")
        finally:
            _close_quietly(lock_file)


def record_sync(project_name: str, home: Path | None = None) -> None:
    path = state_path(home)
    with _locked(path):
        state = _load(path)
        last_sync = state.get("last_sync", {})
        last_sync[project_name] = datetime.now(UTC).isoformat()
        state["last_sync"] = last_sync
        _save(path, state)


def read_sync_timestamps(home: Path | None = None) -> dict[str, str]:
    state = _load(state_path(home))
    return dict(state.get("last_sync", {}))


def forget_sync(project_name: str, home: Path | None = None) -> None:
    """Drop the project's sync record, for a purge verified complete (#2479).

    The record outlived the graph before: `cgr status` kept listing a
    project that no longer exists, with a recent `last sync` and nothing
    else, which reads as healthier than the `(missing)` state it replaced.
    """
    path = state_path(home)
    with _locked(path):
        state = _load(path)
        last_sync = state.get("last_sync", {})
        if project_name not in last_sync:
            return
        del last_sync[project_name]
        state["last_sync"] = last_sync
        _save(path, state)
