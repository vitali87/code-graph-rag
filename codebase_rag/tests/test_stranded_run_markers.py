# A full rebuild retires the incomplete-run markers of runs it supersedes
# (review of PR #3493). An MCP server marks with a run id of its own and
# only that run cleared it, so one whose process died after its first write
# left the project refused for good: by its reads, by `cgr rename` and
# `cgr change-signature`, and in `cgr status`. The CLI sync's side is in
# test_cli_sync_incomplete_marker.py and test_status_syncs_from_graph.py.

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.sync_guard import StrandedRun, retire_stranded_runs, stranded_runs
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.utils.process_owner import process_host

DEAD_RUN = "mcp-dead"


@pytest.fixture(autouse=True)
def _identified_process_table() -> Iterator[None]:
    """A host whose name tells its process table apart, as Linux's does.

    On macOS and Windows the name is the hostname alone, so nothing is
    stranded there; these tests are about what follows once a run is.
    """
    with patch("codebase_rag.sync_guard.process_table_identified", return_value=True):
        yield


def _gone_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _marker(run_id: str, pid: int | None, host: str | None = None) -> ResultRow:
    """A marker row as `CYPHER_STRANDED_RUN_IDS` returns it."""
    return {
        cs.KEY_RUN_ID: run_id,
        cs.KEY_OWNER_HOST: process_host() if host is None else host,
        cs.KEY_OWNER_PID: pid,
    }


class Store:
    """The ingestor double: the other runs' markers, and every write recorded."""

    def __init__(self, stranded: list[ResultRow]) -> None:
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
            return self.stranded
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
    dead = _gone_pid()
    store = Store([_marker(DEAD_RUN, dead)])
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
    assert queries.index(cq.CYPHER_RETIRE_STRANDED_RUN) > queries.index(
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE
    )
    # Deleted only while the marker still names the process that exited.
    assert store.writes[-1] == (
        cq.CYPHER_RETIRE_STRANDED_RUN,
        {cs.KEY_PROJECT_NAME: derive_project_name(project_root)}
        | _marker(DEAD_RUN, dead),
    )


async def test_the_servers_marker_names_its_process(project_root: Path) -> None:
    # Review of PR #3496: what lets a later rebuild tell a run whose process
    # exited from one still writing.
    store = Store([])
    registry = _registry(project_root, store)

    with patch("codebase_rag.mcp.tools.GraphUpdater"):
        await registry.update_repository()

    marks = [
        params
        for query, params in store.writes
        if query == cq.CYPHER_MARK_RUN_INCOMPLETE
    ]
    assert marks
    for params in marks:
        assert params[cs.KEY_RUN_ID] == registry._run_id
        assert params[cs.KEY_OWNER_HOST] == process_host()
        assert params[cs.KEY_OWNER_PID] == os.getpid()


async def test_an_update_with_nothing_stranded_stays_incremental(
    project_root: Path,
) -> None:
    store = Store([])
    registry = _registry(project_root, store)

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        await registry.update_repository()

    updater_cls.return_value.run.assert_called_once_with(force=False)
    assert cq.CYPHER_RETIRE_STRANDED_RUN not in store.queries()


async def test_an_update_that_fails_retires_nothing(project_root: Path) -> None:
    store = Store([_marker(DEAD_RUN, _gone_pid())])
    registry = _registry(project_root, store)

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        updater_cls.return_value.run.side_effect = RuntimeError("update died")
        result = await registry.update_repository()

    assert "update died" in result
    assert cq.CYPHER_RETIRE_STRANDED_RUN not in store.queries()


async def test_an_update_leaves_a_run_whose_process_still_runs(
    project_root: Path,
) -> None:
    # Review of PR #3496: the checkout's lock holds off only this checkout's
    # writers under this CGR_HOME. A run of the project from anywhere else may
    # still be writing, and would lose the marker guarding what it leaves.
    store = Store([_marker("mcp-elsewhere", os.getppid())])
    registry = _registry(project_root, store)

    with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
        await registry.update_repository()

    updater_cls.return_value.run.assert_called_once_with(force=False)
    assert cq.CYPHER_RETIRE_STRANDED_RUN not in store.queries()


async def test_a_reindex_retires_the_runs_it_supersedes(project_root: Path) -> None:
    store = Store([_marker(DEAD_RUN, _gone_pid())])
    registry = _registry(project_root, store)

    with (
        patch("codebase_rag.mcp.tools.GraphUpdater"),
        patch("codebase_rag.mcp.tools.delete_project_embeddings"),
    ):
        result = await registry.index_repository()

    assert "Error" not in result, result
    queries = store.queries()
    assert queries.index(cq.CYPHER_RETIRE_STRANDED_RUN) > queries.index(
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE
    )


async def test_a_reindex_that_fails_retires_nothing(project_root: Path) -> None:
    store = Store([_marker(DEAD_RUN, _gone_pid())])
    registry = _registry(project_root, store)

    with (
        patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls,
        patch("codebase_rag.mcp.tools.delete_project_embeddings"),
    ):
        updater_cls.return_value.run.side_effect = RuntimeError("index died")
        await registry.index_repository()

    assert cq.CYPHER_RETIRE_STRANDED_RUN not in store.queries()


# --- the helpers ----------------------------------------------------------------


def test_stranded_runs_are_the_other_runs_whose_process_exited() -> None:
    dead = _gone_pid()
    store = Store(
        [
            _marker("dead", dead),
            _marker("live", os.getppid()),
            _marker("mine", os.getpid()),
            _marker("far", dead, host="another-machine"),
            {cs.KEY_RUN_ID: "old"},
        ]
    )

    assert stranded_runs(store.ingestor, "proj", "own") == [
        StrandedRun("dead", process_host(), dead)
    ]
    assert store.reads == [{cs.KEY_PROJECT_NAME: "proj", cs.KEY_RUN_ID: "own"}]


def test_where_the_process_table_cannot_be_told_apart_nothing_is_stranded() -> None:
    # Review of PR #3496: under a hostname alone, a run on another machine of
    # the same name reads as local, and its pid, absent here, as exited.
    store = Store([_marker(DEAD_RUN, _gone_pid())])

    with patch("codebase_rag.sync_guard.process_table_identified", return_value=False):
        assert stranded_runs(store.ingestor, "proj", "own") == []


def test_a_store_that_cannot_answer_strands_nothing() -> None:
    # The rebuild then runs as it did before, and the markers stay for the
    # next one rather than being cleared on a guess.
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = ConnectionError("graph unreachable")

    assert stranded_runs(ingestor, "proj", "own") == []


def test_retiring_no_runs_writes_nothing() -> None:
    ingestor = MagicMock()

    retire_stranded_runs(ingestor, "proj", [])

    ingestor.execute_write.assert_not_called()


def test_a_retire_that_fails_does_not_fail_the_rebuild() -> None:
    ingestor = MagicMock()
    ingestor.execute_write.side_effect = ConnectionError("graph unreachable")

    retire_stranded_runs(ingestor, "proj", [StrandedRun(DEAD_RUN, "host", 4242)])

    ingestor.execute_write.assert_called_once()


def test_each_run_is_retired_against_the_process_it_named() -> None:
    # The CLI sync's run id is shared by every CLI writer of the project, so
    # one that started since re-marked it under its own process and keeps it.
    store = Store([])
    runs = [StrandedRun("run-a", "host", 4242), StrandedRun("run-b", "host", 4243)]

    retire_stranded_runs(store.ingestor, "proj", runs)

    assert store.writes == [
        (
            cq.CYPHER_RETIRE_STRANDED_RUN,
            {
                cs.KEY_PROJECT_NAME: "proj",
                cs.KEY_RUN_ID: run.run_id,
                cs.KEY_OWNER_HOST: run.owner_host,
                cs.KEY_OWNER_PID: run.owner_pid,
            },
        )
        for run in runs
    ]
