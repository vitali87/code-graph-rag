# The in-sync check re-hashes a cached file whose mtime moved past the cache
# through an unguarded read, so a cached file that had become unreadable
# raised PermissionError out of `run()` instead of being counted unreadable
# the way the batch pass counts it (issue #1992).
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


def _make_updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    )


def test_a_cached_file_that_became_unreadable_does_not_end_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    (root / "b.py").write_text("def b():\n    return 2\n")
    store = _StatefulIngestor()
    _make_updater(root, store).run(force=True)
    assert _make_updater(root, store)._is_already_in_sync() is True

    # Edited past the cache, so the check must hash it, and unreadable.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    edited = root / "b.py"
    edited.write_text("def b():\n    return 3\n")
    os.utime(edited, (cache_mtime + 1, cache_mtime + 1))

    def unreadable(path: Path) -> str:
        if path.name == "b.py":
            raise PermissionError(13, "Permission denied", str(path))
        return gu.hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()

    monkeypatch.setattr(gu, "_hash_file", unreadable)
    real_with_bytes = gu._hash_file_with_bytes
    monkeypatch.setattr(
        gu,
        "_hash_file_with_bytes",
        lambda path: None if path.name == "b.py" else real_with_bytes(path),
    )
    updater = _make_updater(root, store)
    assert updater._is_already_in_sync() is False
    updater.run()  # completes: the batch pass counts b.py unreadable
    assert "b.py" not in updater._reparsed_file_keys
    assert updater.skipped_because_in_sync is False
    # The persisted cache carries the unreadable mark for the file, neither
    # digest, so the next run retries it (issue #1983). Exact equality tells
    # the mark from a MISSING key and from any stale digest, which the
    # earlier `not in {old, new}` form could not (bot review).
    cache = json.loads((root / cs.HASH_CACHE_FILENAME).read_text())
    assert cache.get("b.py") == cs.HASH_CACHE_UNREADABLE, cache
    # The readable sibling is still cached, so the mark above is the
    # unreadable file being recorded and not a cache rebuilt from nothing.
    old_digest = hashlib.md5(
        b"def b():\n    return 2\n", usedforsecurity=False
    ).hexdigest()
    new_digest = hashlib.md5(edited.read_bytes(), usedforsecurity=False).hexdigest()
    assert set(cache) == {"a.py", "b.py"}, cache
    assert cache["a.py"] not in {old_digest, new_digest}, cache
