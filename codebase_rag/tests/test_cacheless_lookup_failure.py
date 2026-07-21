# A full build asks the graph which of this project's modules already exist
# so their subtrees are deleted before reingest. A sink that claims
# readability but fails the query leaves the graph state UNKNOWN: treating it
# as empty skips every delete and recreates the stale-accumulation corruption
# the probe exists to prevent. The only safe answer is to delete-before-
# reingest every current file (deleting an absent module is a no-op).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater


def test_module_path_lookup_failure_forces_delete_before_reingest(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_PROJECT_MODULE_PATHS:
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    deleted_paths = {
        c.args[1][cs.KEY_PATH]
        for c in mock_ingestor.execute_write.call_args_list
        if c.args[0] == cs.CYPHER_DELETE_MODULE
    }
    assert "m.py" in deleted_paths, deleted_paths
