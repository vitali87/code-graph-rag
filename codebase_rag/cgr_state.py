from __future__ import annotations

import json
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
        if isinstance(data, dict):
            return _StateShape(last_sync=data.get("last_sync", {}))
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


def record_sync(project_name: str, home: Path | None = None) -> None:
    path = state_path(home)
    state = _load(path)
    last_sync = state.get("last_sync", {})
    last_sync[project_name] = datetime.now(UTC).isoformat()
    state["last_sync"] = last_sync
    _save(path, state)


def read_sync_timestamps(home: Path | None = None) -> dict[str, str]:
    state = _load(state_path(home))
    return dict(state.get("last_sync", {}))
