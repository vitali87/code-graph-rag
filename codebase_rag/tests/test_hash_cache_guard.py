# The cache-invalidation preflight asks the graph whether the project still
# exists; a graph that cannot answer (connection refused, a sink that rejects
# reads) must fail OPEN: keep the cache, keep syncing. A raised query error
# aborted the whole non-forced sync instead.
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater


def test_query_failure_keeps_cache_and_sync(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    cache_path = temp_repo / cs.HASH_CACHE_FILENAME
    cache_path.write_text(json.dumps({"m.py": "stale"}), encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_COUNT_PROJECT_MODULES:
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    assert cache_path.is_file()
