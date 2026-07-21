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
