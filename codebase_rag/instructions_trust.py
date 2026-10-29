# Approvals of a repository's .cgr.md, keyed by the file's path and pinned
# to its content. The file is written by whoever can commit to the
# repository, and it reaches the agent's system prompt, so it loads only in
# the exact form the user approved (issue #3456).

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import logs


def instructions_digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def approved_digest(store: Path, instructions: Path) -> str | None:
    """The digest the user approved for `instructions`, if any."""
    return _read_store(store).get(str(instructions.resolve()))


def approve(store: Path, instructions: Path, digest: str) -> None:
    approvals = _read_store(store)
    approvals[str(instructions.resolve())] = digest
    store.parent.mkdir(mode=cs.TRUST_STORE_DIR_MODE, parents=True, exist_ok=True)
    # Replaced whole rather than rewritten in place, so a crash mid-write
    # never leaves a truncated store that would approve nothing; owner-only,
    # since it records which repositories the user works on.
    fd, temp = tempfile.mkstemp(dir=store.parent, prefix=store.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(approvals, f, indent=cs.JSON_INDENT, sort_keys=True)
        os.chmod(temp, cs.TRUST_STORE_FILE_MODE)
        os.replace(temp, store)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _read_store(store: Path) -> dict[str, str]:
    if not store.is_file():
        return {}
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # Unreadable is treated as empty: nothing is approved, so the worst
        # case is being asked again, never loading an unapproved file.
        logger.warning(
            logs.CGR_INSTRUCTIONS_STORE_UNREADABLE.format(path=store, error=e)
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        path: digest
        for path, digest in data.items()
        if isinstance(path, str) and isinstance(digest, str)
    }
