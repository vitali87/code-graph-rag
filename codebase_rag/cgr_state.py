from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from loguru import logger

from .config import settings

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


@contextmanager
def _locked(path: Path):
    """Serialize whole-file read-modify-writes on the state (#2479).

    Every writer rewrites the file from a read of it, so two concurrent
    writers can silently drop each other's update -- a sync finishing while
    a prune forgets the purged project would resurrect the ghost record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f"{path.name}.lock"
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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
