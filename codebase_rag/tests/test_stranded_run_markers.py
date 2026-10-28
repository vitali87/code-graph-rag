# A full rebuild retires the incomplete-run markers of runs it supersedes
# (review of PR #3493). An MCP server marks with a run id of its own and
# only that run cleared it, so one whose process died after its first write
# left the project refused for good: by its reads, by `cgr rename` and
# `cgr change-signature`, and in `cgr status`. The CLI sync's side is in
# test_cli_sync_incomplete_marker.py and test_status_syncs_from_graph.py.

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.sync_guard import retire_stranded_runs, stranded_runs
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.path_utils import derive_project_name

DEAD_RUN = "mcp-dead"


class Store:
    """The ingestor double: one stranded run, and every marker write recorded."""

    def __init__(self, stranded: list[str]) -> None:
        self.stranded = stranded
        self.reads: list[PropertyDict] = []
        self.writes: list[tuple[str, PropertyDict]] = []
        self.ingestor = MagicMock()
        self.ingestor.fetch_all.side_effect = self.fetch_all
        self.ingestor.execute_write.side_effect = self.execute_write

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_STRANDED_RUN_IDS:
            self.reads.append(dict(params or {}))
            return [{cs.KEY_RUN_ID: run_id} for run_id in self.stranded]
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append((query, dict(params or {})))

    def queries(self) -> list[str]:
        return [query for query, _params in self.writes]


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    return tmp_path


def _registry(root: Path, store: Store) -> MCPToolsRegistry:
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=store.ingestor, cypher_gen=MagicMock()
    )
    store.ingestor.list_projects.return_value = [derive_project_name(root)]
    return registry


# --- the MCP server's full update and re-index ----------------------------------


async def test_an_update_over_a_stranded_run_rebuilds_then_retires_it(
    project_root: Path,
) -> None:
    store = Store([DEAD_RUN])
    registry = _registry(project_root, store)

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        result = await registry.update_repository()

    assert "Error" not in result, result
    # Forced: an incremental run skips the unchanged dependents a stopped
    # re-ingest may have deleted.
    updater_cls.return_value.run.assert_called_once_with(force=True)
    # Its own run id is not stranded: that marker is this update's to clear.
    assert store.reads == [
        {
            cs.KEY_PROJECT_NAME: derive_project_name(project_root),
            cs.KEY_RUN_ID: registry._run_id,
        }
    ]
    queries = store.queries()
    assert queries.index(cq.CYPHER_RETIRE_STRANDED_RUNS) > queries.index(
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE
    )
    assert store.writes[-1] == (
        cq.CYPHER_RETIRE_STRANDED_RUNS,
        {
            cs.KEY_PROJECT_NAME: derive_project_name(project_root),
            cs.KEY_RUN_IDS: [DEAD_RUN],
        },
    )


async def test_an_update_with_nothing_stranded_stays_incremental(
    project_root: Path,
) -> None:
    store = Store([])
    registry = _registry(project_root, store)

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        await registry.update_repository()

    updater_cls.return_value.run.assert_called_once_with(force=False)
    assert cq.CYPHER_RETIRE_STRANDED_RUNS not in store.queries()


async def test_an_update_that_fails_retires_nothing(project_root: Path) -> None:
    store = Store([DEAD_RUN])
    registry = _registry(project_root, store)

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        updater_cls.return_value.run.side_effect = RuntimeError("update died")
        result = await registry.update_repository()

    assert "update died" in result
    assert cq.CYPHER_RETIRE_STRANDED_RUNS not in store.queries()


async def test_a_reindex_retires_the_runs_it_supersedes(project_root: Path) -> None:
    store = Store([DEAD_RUN])
    registry = _registry(project_root, store)

    with (
        patch("codebase_rag.mcp.tools.GraphUpdater"),
        patch("codebase_rag.mcp.tools.delete_project_embeddings"),
    ):
        result = await registry.index_repository()

    assert "Error" not in result, result
    queries = store.queries()
    assert queries.index(cq.CYPHER_RETIRE_STRANDED_RUNS) > queries.index(
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE
    )


async def test_a_reindex_that_fails_retires_nothing(project_root: Path) -> None:
    store = Store([DEAD_RUN])
    registry = _registry(project_root, store)

    with (
        patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls,
        patch("codebase_rag.mcp.tools.delete_project_embeddings"),
    ):
        updater_cls.return_value.run.side_effect = RuntimeError("index died")
        await registry.index_repository()

    assert cq.CYPHER_RETIRE_STRANDED_RUNS not in store.queries()


# --- the helpers ----------------------------------------------------------------


def test_stranded_runs_reads_the_other_runs_that_wrote() -> None:
    store = Store(["a", "b"])

    assert stranded_runs(store.ingestor, "proj", "mine") == ["a", "b"]
    assert store.reads == [{cs.KEY_PROJECT_NAME: "proj", cs.KEY_RUN_ID: "mine"}]


def test_a_store_that_cannot_answer_strands_nothing() -> None:
    # The rebuild then runs as it did before, and the markers stay for the
    # next one rather than being cleared on a guess.
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = ConnectionError("graph unreachable")

    assert stranded_runs(ingestor, "proj", "mine") == []


def test_retiring_no_runs_writes_nothing() -> None:
    ingestor = MagicMock()

    retire_stranded_runs(ingestor, "proj", [])

    ingestor.execute_write.assert_not_called()


def test_a_retire_that_fails_does_not_fail_the_rebuild() -> None:
    ingestor = MagicMock()
    ingestor.execute_write.side_effect = ConnectionError("graph unreachable")

    retire_stranded_runs(ingestor, "proj", [DEAD_RUN])

    ingestor.execute_write.assert_called_once()
