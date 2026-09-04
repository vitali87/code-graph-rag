# The cache-invalidation preflight asks the graph whether the project still
# exists; a graph that cannot answer (connection refused, a sink that rejects
# reads) must fail OPEN: keep the cache, keep syncing. A raised query error
# aborted the whole non-forced sync instead.
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as graph_updater_module
from codebase_rag.tests.conftest import create_and_run_updater


def test_query_failure_keeps_cache_and_sync(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    cache_path = temp_repo / cs.HASH_CACHE_FILENAME
    cache_path.write_text(json.dumps({"m.py": "stale"}), encoding="utf-8")
    # Backdate the cache so the mtime fast path cannot skip the source file
    # before the hash comparison sees the stale entry.
    stale_time = cache_path.stat().st_mtime - 60
    os.utime(cache_path, (stale_time, stale_time))

    count_queries: list[str] = []

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_COUNT_PROJECT_MODULES:
            count_queries.append(query)
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    # The guard must actually have asked (and been refused), the cache must
    # survive, and the sync must still ingest the repo's module.
    assert count_queries
    assert cache_path.is_file()
    module_qns = {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.MODULE
    }
    assert any(str(qn).endswith(".m") for qn in module_qns), module_qns


def test_a_cache_that_cannot_be_discarded_does_not_end_the_run(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1647: the discard is best-effort, so a refused unlink must not raise.

    Every other filesystem writer on this path swallows `OSError` and degrades,
    so a read-only tree reaches the discard having survived all of them. Losing
    the whole indexing run to a file the tool only wanted to DELETE is the
    worst of both: no index, and the stale cache still there.
    """
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    cache_path = temp_repo / cs.HASH_CACHE_FILENAME
    cache_path.write_text(json.dumps({"m.py": "stale"}), encoding="utf-8")
    mtimes_path = temp_repo / cs.DIR_MTIMES_FILENAME
    mtimes_path.write_text(json.dumps({".": 1.0}), encoding="utf-8")
    # Stamp the cache AFTER the sources, which is what a real completed run
    # leaves behind (`os.utime(observed_at)` at the commit point). Backdating
    # it here would hide the defect this test exists to catch: the mtime fast
    # path skips a file before its hash is compared, so a surviving cache
    # makes every file look unchanged against hashes known dead, and the run
    # indexes NOTHING -- on every subsequent run, because the graph stays
    # empty. Verified: with the cache backdated this test passes even when the
    # run indexes nothing in production (issue #1647).
    fresh_time = cache_path.stat().st_mtime + 5
    os.utime(cache_path, (fresh_time, fresh_time))

    # An empty project is what makes the graph look wiped, which is the only
    # branch that reaches the discard at all.
    def empty_project(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_COUNT_PROJECT_MODULES:
            return [{"count": 0}]
        return []

    mock_ingestor.fetch_all.side_effect = empty_project

    real_unlink = Path.unlink
    refused: list[str] = []

    def _refuse(self: Path, missing_ok: bool = False) -> None:
        if self.name in (cs.HASH_CACHE_FILENAME, cs.DIR_MTIMES_FILENAME):
            refused.append(self.name)
            raise OSError(errno.EROFS, "Read-only file system")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _refuse)

    warnings: list[str] = []
    sink_id = graph_updater_module.logger.add(warnings.append, level="WARNING")
    try:
        create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)
    finally:
        graph_updater_module.logger.remove(sink_id)

    monkeypatch.undo()

    # Without this the run could complete for the trivial reason that it never
    # reached the discard, and the assertion below would prove nothing. BOTH
    # files must be attempted: guarding only the first would leave the second
    # unguarded and still raise.
    assert refused == [cs.HASH_CACHE_FILENAME, cs.DIR_MTIMES_FILENAME], refused

    # The run must still have indexed the repo, which is the whole point of
    # degrading rather than raising. The cache entry is a hash that cannot
    # match `m.py`, so the file is re-read on its own merits; asserting this
    # with a MATCHING hash would pass for the wrong reason, since the run
    # would skip the file as unchanged rather than because the discard failed.
    module_qns = {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.MODULE
    }
    assert any(str(qn).endswith(".m") for qn in module_qns), module_qns

    # The stale cache is still on disk, which is the documented cost of the
    # degraded path. The INDEX is not lost, because the run ignores the cache
    # in memory once the discard fails.
    assert cache_path.is_file()

    # Both refusals must be reported by name. Without this, deleting the log
    # line and swallowing silently keeps every assertion above green, so
    # nothing pins the new constant or names the file a user must remove.
    discard_warnings = [w for w in warnings if "Could not discard" in w]
    assert len(discard_warnings) == 2, warnings
    assert any(cs.HASH_CACHE_FILENAME in w for w in discard_warnings), warnings
    assert any(cs.DIR_MTIMES_FILENAME in w for w in discard_warnings), warnings
