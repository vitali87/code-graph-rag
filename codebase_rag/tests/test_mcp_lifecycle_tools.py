"""`LifecycleTools`, the MCP handlers that write the graph, driven on the
mixin itself over the dict-backed `marker_store`. Pinned directly: the
order each writer marks, purges, deletes and clears in, which is the
incomplete-run invariants `run_marker.py` states ((a) no destructive step
before the durable mark, (b) no success before the clear, (c) the scoped
reingest marks before it migrates), and what each failure leaves behind: a
marker that comes off when nothing was written, one that stays when
something was, and the in-process damage a wipe or a delta records."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import tool_errors as te
from codebase_rag.exceptions import ReingestAborted
from codebase_rag.mcp import constants as mcs
from codebase_rag.mcp.lifecycle_tools import LifecycleTools
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.tests.conftest import marker_store
from codebase_rag.types_defs import ReingestReport
from codebase_rag.utils.path_utils import derive_project_name

pytestmark = [pytest.mark.anyio]

_LIFECYCLE = "codebase_rag.mcp.lifecycle_tools"
OTHER = "other__0000aaaa"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _tools(root: Path, store: MagicMock) -> LifecycleTools:
    tools = LifecycleTools()
    tools.project_root = str(root)
    tools.ingestor = store
    tools.workspace = None
    tools._ingestor_lock = asyncio.Lock()
    tools._live_updater = None
    tools.marker = RunMarker(lambda: tools.ingestor)
    # The updater is replaced in every test that builds one.
    tools.parsers, tools.queries = {}, {}
    return tools


def _recording(store: MagicMock) -> list[str]:
    """The marker writes, deletes and migrations `store` sees, in order."""
    events: list[str] = []
    write = store.execute_write.side_effect
    delete = store.delete_project.side_effect

    def record_write(query: str, params: dict | None = None) -> None:
        if query == cq.CYPHER_MARK_PROJECT_INCOMPLETE:
            events.append("writing" if (params or {})[cs.KEY_WRITING] else "mark")
        elif query == cq.CYPHER_CLEAR_PROJECT_INCOMPLETE:
            events.append("clear")
        write(query, params)

    def record_delete(name: str) -> None:
        events.append("delete")
        delete(name)

    store.execute_write.side_effect = record_write
    store.delete_project.side_effect = record_delete
    store.ensure_constraints.side_effect = lambda: events.append("constraints")
    return events


def _marked(store: MagicMock, project: str) -> bool:
    return bool(store._marker_store.get(project))


class _Updater:
    """A scoped updater that writes after its read-only prologue, as the
    real one does, then answers or fails."""

    def __init__(
        self,
        project_name: str,
        events: list[str] | None = None,
        *,
        fail: BaseException | None = None,
        writes: bool = True,
    ) -> None:
        self.project_name = project_name
        self.events = events if events is not None else []
        self.fail = fail
        self.writes = writes
        self.reingest_mutated = False

    def reingest(
        self,
        paths: list[str],
        deleted: list[str] | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> ReingestReport:
        if self.writes:
            if before_write is not None:
                before_write()
            self.reingest_mutated = True
            self.events.append("reingest")
        if self.fail is not None:
            raise self.fail
        return ReingestReport(
            reparsed=tuple(paths),
            affected=("b.py",),
            removed=tuple(deleted or ()),
            skipped=(),
            elapsed_ms=1.234,
        )


# --- list_projects --------------------------------------------------------------


async def test_list_projects_answers_the_graphs_projects(tmp_path: Path) -> None:
    store = marker_store()
    store.list_projects.return_value = ["a", "b"]

    assert await _tools(tmp_path, store).list_projects() == {
        "projects": ["a", "b"],
        "count": 2,
    }


async def test_a_list_projects_failure_is_an_empty_answer_with_the_error(
    tmp_path: Path,
) -> None:
    store = marker_store()
    store.list_projects.side_effect = RuntimeError("store down")

    assert await _tools(tmp_path, store).list_projects() == {
        "error": "store down",
        "projects": [],
        "count": 0,
    }


# --- delete_project ---------------------------------------------------------------


async def test_deleting_an_unknown_project_touches_nothing(tmp_path: Path) -> None:
    store = marker_store()
    store.list_projects.return_value = [OTHER]
    events = _recording(store)

    result = await _tools(tmp_path, store).delete_project("ghost")

    assert result["success"] is False
    assert "ghost" in result["error"]
    assert events == []


async def test_a_delete_marks_before_purging_and_clears_only_after(
    tmp_path: Path,
) -> None:
    store = marker_store()
    store.list_projects.return_value = [OTHER]
    events = _recording(store)
    tools = _tools(tmp_path, store)

    with patch(
        f"{_LIFECYCLE}.delete_project_embeddings",
        side_effect=lambda *_a: events.append("purge"),
    ):
        result = await tools.delete_project(OTHER)

    # The mark says `writing=false` until the purge, which is the first
    # step a fresh process cannot simply clear and carry on from.
    assert events == ["mark", "writing", "purge", "delete", "clear"]
    assert result == {
        "success": True,
        "project": OTHER,
        "message": mcs.MCP_PROJECT_DELETED.format(project_name=OTHER),
    }
    assert not _marked(store, OTHER)
    assert tools.marker.partial_graph_refusal(OTHER, cs.MCPToolName.CALLERS) is None


async def test_a_delete_whose_mark_fails_purges_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    store = marker_store()
    store.list_projects.return_value = [OTHER]
    store._failing.add("mark")
    events = _recording(store)

    with patch(f"{_LIFECYCLE}.delete_project_embeddings") as purge:
        result = await _tools(tmp_path, store).delete_project(OTHER)

    assert result == {
        "success": False,
        "error": mcs.MCP_INCOMPLETE_MARKER_REQUIRED.format(project=OTHER),
    }
    purge.assert_not_called()
    assert "delete" not in events


async def test_a_delete_whose_clear_fails_is_not_reported_done(tmp_path: Path) -> None:
    store = marker_store()
    store.list_projects.return_value = [OTHER]
    store._failing.add("clear")

    with patch(f"{_LIFECYCLE}.delete_project_embeddings"):
        result = await _tools(tmp_path, store).delete_project(OTHER)

    assert result == {
        "success": False,
        "error": mcs.MCP_INCOMPLETE_MARKER_STUCK.format(project=OTHER),
    }
    assert _marked(store, OTHER)


# --- wipe_database -----------------------------------------------------------------


async def test_a_wipe_without_confirmation_does_nothing(tmp_path: Path) -> None:
    store = marker_store()

    assert await _tools(tmp_path, store).wipe_database(False) == (
        mcs.MCP_WIPE_CANCELLED
    )
    store.clean_database.assert_not_called()


async def test_a_completed_wipe_settles_earlier_damage(tmp_path: Path) -> None:
    store = marker_store()
    tools = _tools(tmp_path, store)
    tools.marker.invalidate(OTHER)

    with patch(f"{_LIFECYCLE}.clear_all_embeddings"):
        assert await tools.wipe_database(True) == mcs.MCP_WIPE_SUCCESS

    assert tools.marker.partial_graph_refusal(OTHER, cs.MCPToolName.CALLERS) is None


async def test_a_failed_wipe_blocks_every_project_until_a_wipe_completes(
    tmp_path: Path,
) -> None:
    store = marker_store()
    store.clean_database.side_effect = RuntimeError("disk full")
    tools = _tools(tmp_path, store)

    result = await tools.wipe_database(True)

    assert isinstance(result, te.ToolFailure)
    assert result == mcs.MCP_WIPE_ERROR.format(error="disk full")
    # A wipe writes no marker and spans every project, so one project's
    # completed run cannot vouch for the rest.
    assert tools.marker.require(OTHER) is None
    assert tools.marker.require_cleared(OTHER) is None
    for project in (OTHER, "never_seen"):
        assert tools.marker.partial_graph_refusal(project, cs.MCPToolName.CALLERS)

    store.clean_database.side_effect = None
    with patch(f"{_LIFECYCLE}.clear_all_embeddings"):
        assert await tools.wipe_database(True) == mcs.MCP_WIPE_SUCCESS
    for project in (OTHER, "never_seen"):
        assert (
            tools.marker.partial_graph_refusal(project, cs.MCPToolName.CALLERS) is None
        )


# --- reingest ----------------------------------------------------------------------


async def test_a_reingest_of_an_unindexed_project_refuses_without_a_marker(
    tmp_path: Path,
) -> None:
    store = marker_store()
    store.list_projects.return_value = []
    project = derive_project_name(tmp_path)

    result = await _tools(tmp_path, store).reingest(["a.py"])

    assert result["error"] == mcs.MCP_REINGEST_ERROR.format(
        error=mcs.MCP_REINGEST_NEEDS_INDEX.format(project=project)
    )
    assert result["reparsed"] == []
    assert not _marked(store, project)


async def test_a_reingest_refuses_a_graph_another_process_left_partial(
    tmp_path: Path,
) -> None:
    project = derive_project_name(tmp_path)
    store = marker_store()
    store.list_projects.return_value = [project]
    # Another registry over the same store marked the project and died.
    assert RunMarker(lambda: store).require(project) is None
    tools = _tools(tmp_path, store)

    with patch(f"{_LIFECYCLE}.GraphUpdater") as updater:
        result = await tools.reingest(["a.py"])

    assert result["error"] == mcs.MCP_REINGEST_ERROR.format(
        error=mcs.MCP_REINGEST_AFTER_FAILED_RUN.format(project=project)
    )
    updater.assert_not_called()
    assert _marked(store, project)


async def test_a_retained_reingest_marks_around_its_writes(tmp_path: Path) -> None:
    project = derive_project_name(tmp_path)
    store = marker_store()
    events = _recording(store)
    tools = _tools(tmp_path, store)
    tools._live_updater = _Updater(project, events)

    result = await tools.reingest(["a.py"], deleted=["gone.py"])

    assert events == ["mark", "writing", "reingest", "clear"]
    assert result == {
        "reparsed": ["a.py"],
        "affected": ["b.py"],
        "removed": ["gone.py"],
        "skipped": [],
        "elapsed_ms": 1.2,
    }
    assert not _marked(store, project)


async def test_a_reingest_that_fails_before_writing_lifts_its_marker(
    tmp_path: Path,
) -> None:
    project = derive_project_name(tmp_path)
    store = marker_store()
    tools = _tools(tmp_path, store)
    updater = _Updater(project, fail=ReingestAborted("bad path"), writes=False)
    tools._live_updater = updater

    result = await tools.reingest(["a.py"])

    assert "bad path" in result["error"]
    # Nothing changed: the graph is whole and the warm updater still holds.
    assert not _marked(store, project)
    assert tools._live_updater is updater
    assert tools.marker.partial_graph_refusal(project, cs.MCPToolName.CALLERS) is None


async def test_a_reingest_that_fails_after_writing_keeps_its_marker(
    tmp_path: Path,
) -> None:
    project = derive_project_name(tmp_path)
    store = marker_store()
    tools = _tools(tmp_path, store)
    tools._live_updater = _Updater(project, fail=RuntimeError("write lost"))

    result = await tools.reingest(["a.py"])

    assert "write lost" in result["error"]
    assert tools._live_updater is None
    assert tools.marker.partial_graph_refusal(project, cs.MCPToolName.CALLERS)
    # The record a restarted process reads: a run that reached its writes.
    assert RunMarker(lambda: store).persisted_incomplete(project) is True


# --- index_repository / update_repository ----------------------------------------


def _updater_class(events: list[str], fail: BaseException | None = None) -> type:
    class Built:
        def __init__(self, **kwargs: object) -> None:
            self.project_name = kwargs["project_name"]

        def run(self) -> None:
            events.append("run")
            if fail is not None:
                raise fail

    return Built


async def test_an_index_marks_purges_deletes_rebuilds_then_clears(
    tmp_path: Path,
) -> None:
    project = derive_project_name(tmp_path)
    store = marker_store()
    store.list_projects.return_value = [project]
    events = _recording(store)
    tools = _tools(tmp_path, store)

    with (
        patch(f"{_LIFECYCLE}.GraphUpdater", _updater_class(events)),
        patch(
            f"{_LIFECYCLE}.delete_project_embeddings",
            side_effect=lambda *_a: events.append("purge"),
        ),
    ):
        result = await tools.index_repository()

    assert events == [
        "mark",
        "writing",
        "purge",
        "delete",
        "constraints",
        "run",
        "clear",
    ]
    assert result == mcs.MCP_INDEX_SUCCESS_PROJECT.format(
        path=str(tmp_path), project_name=project
    )
    assert tools._live_updater is not None
    assert tools._live_updater.project_name == project


async def test_an_update_marks_before_its_migration(tmp_path: Path) -> None:
    store = marker_store()
    events = _recording(store)
    tools = _tools(tmp_path, store)

    with patch(f"{_LIFECYCLE}.GraphUpdater", _updater_class(events)):
        result = await tools.update_repository()

    # An update writes from its first step, so it marks as writing at once.
    assert events == ["writing", "constraints", "run", "clear"]
    assert result == mcs.MCP_UPDATE_SUCCESS.format(path=str(tmp_path))


async def test_an_update_whose_mark_fails_never_migrates(tmp_path: Path) -> None:
    store = marker_store()
    store._failing.add("mark")
    events = _recording(store)
    project = derive_project_name(tmp_path)

    with patch(f"{_LIFECYCLE}.GraphUpdater", _updater_class(events)):
        result = await _tools(tmp_path, store).update_repository()

    assert isinstance(result, te.ToolFailure)
    assert mcs.MCP_INCOMPLETE_MARKER_REQUIRED.format(project=project) in result
    assert "constraints" not in events
    assert "run" not in events


async def test_a_failed_update_leaves_the_project_marked(tmp_path: Path) -> None:
    store = marker_store()
    events = _recording(store)
    project = derive_project_name(tmp_path)
    tools = _tools(tmp_path, store)

    with patch(
        f"{_LIFECYCLE}.GraphUpdater", _updater_class(events, RuntimeError("parse"))
    ):
        result = await tools.update_repository()

    assert result == mcs.MCP_UPDATE_ERROR.format(error="parse")
    assert "clear" not in events
    assert tools._live_updater is None
    assert RunMarker(lambda: store).persisted_incomplete(project) is True


# --- _delta_after_write -----------------------------------------------------------


def test_a_write_to_an_unindexed_project_has_no_delta(tmp_path: Path) -> None:
    store = marker_store()
    store.list_projects.return_value = []
    events = _recording(store)

    assert _tools(tmp_path, store)._delta_after_write(["a.py"]) == ""
    assert events == []


def test_a_completed_delta_is_reported_and_lifts_its_marker(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    project = derive_project_name(tmp_path)
    store = marker_store()
    events = _recording(store)
    tools = _tools(tmp_path, store)
    tools._live_updater = _Updater(project, events)

    delta = tools._delta_after_write(["a.py"])

    assert delta.startswith("\n\n" + mcs.MCP_DELTA_HEADER + "\n{")
    assert events == ["mark", "writing", "reingest", "clear"]
    assert not _marked(store, project)


def test_a_delta_aborted_before_writing_leaves_the_graph_trusted(
    tmp_path: Path,
) -> None:
    project = derive_project_name(tmp_path)
    store = marker_store()
    tools = _tools(tmp_path, store)
    updater = _Updater(project, fail=ValueError("no such path"), writes=False)
    tools._live_updater = updater

    delta = tools._delta_after_write(["a.py"])

    assert delta == "\n\n" + mcs.MCP_DELTA_ERROR.format(error="no such path")
    assert not _marked(store, project)
    assert tools._live_updater is updater
    assert tools.marker.partial_graph_refusal(project, cs.MCPToolName.CALLERS) is None


def test_a_delta_that_failed_mid_write_marks_the_project_damaged(
    tmp_path: Path,
) -> None:
    project = derive_project_name(tmp_path)
    store = marker_store()
    tools = _tools(tmp_path, store)
    tools._live_updater = _Updater(project, fail=RuntimeError("lost"))

    delta = tools._delta_after_write(["a.py"])

    assert delta == "\n\n" + mcs.MCP_DELTA_ERROR.format(error="lost")
    assert tools._live_updater is None
    assert tools.marker.damage.blocks(project)
    # The flag records damage no marker describes: no recovery may heal it.
    assert tools.marker.damage.from_failed_clear is None
