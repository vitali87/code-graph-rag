"""`EditTools`, the MCP `rename` handler (issue #1532), driven on the mixin
itself over a graph indexed from a real snippet, with the incomplete-run
markers kept by `marker_store`. Pinned directly: the payload a preview, an
applied rename and a refusal answer with, the refusal for a project indexed
from another root, and the re-ingest the rename is held to its contract
through: which updater it uses, that it refuses a graph a peer left partial
even when warm, and the marker each outcome leaves."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.editing.rename import RenameReport
from codebase_rag.exceptions import ReingestAborted
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp import constants as mcs
from codebase_rag.mcp.edit_tools import EditTools
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import marker_store
from codebase_rag.types_defs import GraphRefusal, PropertyDict, ReingestReport
from evals.cgr_graph import StatefulIngestor

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("fingerprint_inputs_once_per_session"),
]

PROJECT = "edits"
OTHER = "other"
HELPER = f"{PROJECT}.pkg.util.helper"
FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a, b):\n    return a + b\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1, 2)\n",
    # Not imported: the call binds by name alone, so its site is a guess.
    "pkg/stray.py": "def lonely():\n    return 1\n",
    "pkg/caller.py": "def go():\n    return lonely()\n",
}


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class _Edits(EditTools):
    async def _graph_query[T](
        self,
        tool: cs.MCPToolName,
        project: str | None,
        run: Callable[[str], T | GraphRefusal],
        target: str | None = None,
    ) -> T | GraphRefusal:
        # The registry's dispatcher, reduced to the one thing `rename` takes
        # from it: the project to run for.
        assert tool == cs.MCPToolName.RENAME
        assert project is not None
        return run(project)


class _Indexed:
    """A project indexed from `FILES`, its graph and markers in one store."""

    def __init__(self, root: Path) -> None:
        for rel, text in FILES.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.root = root
        parsers, queries = load_parsers()
        self.graph = StatefulIngestor()
        self.updater = GraphUpdater(
            ingestor=self.graph,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=PROJECT,
        )
        self.updater.run(force=True)
        # The markers live beside the graph, in the same store.
        self.store = marker_store()
        self.store.list_projects.return_value = [PROJECT]
        read_marker = self.store.fetch_all.side_effect

        def fetch_all(query: str, params: PropertyDict | None = None) -> list:
            if "run_incomplete" in query:
                return read_marker(query, params)
            return self.graph.fetch_all(query, None if params is None else dict(params))

        self.store.fetch_all.side_effect = fetch_all
        self.events: list[str] = []
        write = self.store.execute_write.side_effect

        def record(query: str, params: dict | None = None) -> None:
            if query == cq.CYPHER_MARK_PROJECT_INCOMPLETE:
                writing = (params or {})[cs.KEY_WRITING]
                self.events.append("writing" if writing else "mark")
            elif query == cq.CYPHER_CLEAR_PROJECT_INCOMPLETE:
                self.events.append("clear")
            write(query, params)

        self.store.execute_write.side_effect = record

    def tools(self, project_root: Path | None = None) -> _Edits:
        tools = _Edits()
        tools.project_root = str(project_root or self.root)
        tools.ingestor = self.store
        tools.workspace = None
        tools._ingestor_lock = asyncio.Lock()
        tools._live_updater = self.updater
        tools.marker = RunMarker(lambda: tools.ingestor)
        tools.parsers, tools.queries = {}, {}
        return tools

    def marked(self) -> bool:
        return bool(self.store._marker_store.get(PROJECT))

    def sources(self) -> dict[str, str]:
        return {rel: (self.root / rel).read_text(encoding="utf-8") for rel in FILES}


@pytest.fixture
def indexed(temp_repo: Path) -> _Indexed:
    return _Indexed(temp_repo / PROJECT)


class _Updater:
    """A scoped updater that writes after its read-only prologue, as the
    real one does, then answers or fails."""

    def __init__(
        self, *, fail: BaseException | None = None, writes: bool = True
    ) -> None:
        self.project_name = PROJECT
        self.fail = fail
        self.writes = writes
        self.reingest_mutated = False
        self.called = False

    def reingest(
        self, paths: list[str], before_write: Callable[[], None] | None = None
    ) -> ReingestReport:
        self.called = True
        if self.writes and before_write is not None:
            before_write()
            self.reingest_mutated = True
        if self.fail is not None:
            raise self.fail
        return ReingestReport(tuple(paths), (), (), (), 0.0)


# --- the handler's payloads --------------------------------------------------------


async def test_a_preview_reports_the_sites_and_diff_and_writes_nothing(
    indexed: _Indexed,
) -> None:
    before = indexed.sources()

    payload = await indexed.tools().rename(
        HELPER, "assist", dry_run=True, project=PROJECT
    )

    assert isinstance(payload, dict)
    assert payload["applied"] is False
    assert payload["files"] == ("pkg/app.py", "pkg/util.py")
    assert "+def assist(a, b):" in payload["diff"]
    # JSON-ready dicts, as the server sends them.
    assert [(s["kind"], s["path"], s["line"]) for s in payload[cs.KEY_SITES]] == [
        ("definition", "pkg/util.py", 1),
        ("call", "pkg/app.py", 5),
        ("import", "pkg/app.py", 1),
    ]
    assert payload["verdict"] is None
    assert cs.DICT_KEY_WARNING not in payload
    assert indexed.sources() == before
    assert indexed.events == []


async def test_an_applied_rename_is_reingested_inside_its_marker(
    indexed: _Indexed,
) -> None:
    payload = await indexed.tools().rename(HELPER, "assist", project=PROJECT)

    assert isinstance(payload, dict)
    assert payload["applied"] is True, payload["message"]
    assert payload["verdict"]["ok"] is True
    assert "def assist(a, b):" in indexed.sources()["pkg/util.py"]
    assert indexed.events == ["mark", "writing", "clear"]
    assert not indexed.marked()


async def test_a_refused_rename_reports_its_sites_and_writes_nothing(
    indexed: _Indexed,
) -> None:
    before = indexed.sources()

    payload = await indexed.tools().rename(
        f"{PROJECT}.pkg.stray.lonely", "alone", project=PROJECT
    )

    assert isinstance(payload, dict)
    assert cs.MCPParamName.ALLOW_HEURISTIC in payload[cs.DICT_KEY_ERROR]
    assert payload[cs.KEY_AMBIGUOUS] == [
        {
            "kind": "call",
            "path": "pkg/caller.py",
            "line": 2,
            "col": 11,
            "owner": f"{PROJECT}.pkg.caller.go",
            "resolution": cs.EdgeResolution.HEURISTIC.value,
        }
    ]
    assert payload[cs.KEY_UNLOCATABLE] == []
    assert payload[cs.KEY_UNPLANNED] == []
    assert indexed.sources() == before


def test_a_project_indexed_from_another_root_is_not_edited_here(
    indexed: _Indexed, tmp_path: Path
) -> None:
    # Its relative paths may exist under this root too, and name other files.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "pkg").mkdir(parents=True)
    (elsewhere / "pkg" / "util.py").write_text(FILES["pkg/util.py"], encoding="utf-8")

    payload = indexed.tools(elsewhere)._run_rename(
        PROJECT, HELPER, "assist", False, False
    )

    assert payload == {cs.DICT_KEY_ERROR: cs.RENAME_WRONG_ROOT.format(project=PROJECT)}
    assert (elsewhere / "pkg" / "util.py").read_text(encoding="utf-8") == (
        FILES["pkg/util.py"]
    )


@pytest.mark.parametrize(
    ("store_refuses", "warning"),
    [
        (False, None),
        (True, mcs.MCP_INCOMPLETE_MARKER_REQUIRED.format(project=PROJECT)),
    ],
    ids=["marked", "store-refuses"],
)
def test_a_rollback_whose_reingest_failed_marks_the_project_durably(
    indexed: _Indexed, store_refuses: bool, warning: str | None
) -> None:
    # The rename stayed applied but the graph is half restored: a fresh
    # process must refuse it, and when the record cannot be written the
    # caller is told rather than left with a graph that looks whole.
    tools = indexed.tools()
    report = RenameReport(
        qualified_name=HELPER,
        old_name="helper",
        new_name="assist",
        applied=True,
        transaction_id="tx",
        files=("pkg/util.py",),
        sites=(),
        ambiguous=(),
        unlocatable=(),
        doc_mentions=(),
        hierarchy=(HELPER,),
        diff="",
        message="",
        graph_incomplete=True,
    )
    if store_refuses:
        indexed.store._failing.add("mark")

    with patch("codebase_rag.editing.rename.rename", return_value=report):
        payload = tools._run_rename(PROJECT, HELPER, "assist", False, False)

    assert isinstance(payload, dict)
    assert payload.get(cs.DICT_KEY_WARNING) == warning
    assert tools._live_updater is None
    assert tools.marker.damage.blocks(PROJECT)
    assert indexed.marked() is not store_refuses


# --- the re-ingest the rename is measured through -------------------------------


def test_a_project_with_no_graph_has_no_reingest(indexed: _Indexed) -> None:
    tools = indexed.tools()
    tools._live_updater = None

    assert tools._guarded_rename_reingest("never_indexed") is None


def test_a_warm_updater_for_the_project_is_reused(indexed: _Indexed) -> None:
    tools = indexed.tools()

    assert tools._rename_reingest_updater(PROJECT) is indexed.updater


def test_a_warm_updater_for_another_project_is_not_reused(indexed: _Indexed) -> None:
    # It describes PROJECT's graph; another project's rename hydrates its
    # own, marking first as every scoped re-ingest does.
    indexed.store.list_projects.return_value = [PROJECT, OTHER]
    tools = indexed.tools()

    with patch("codebase_rag.mcp.lifecycle_tools.GraphUpdater") as build:
        updater = tools._rename_reingest_updater(OTHER)

    assert updater is build.return_value
    assert build.call_args.kwargs["project_name"] == OTHER
    assert indexed.store._marker_store.get(OTHER) is True


def test_a_warm_updater_still_refuses_a_graph_a_peer_left_partial(
    indexed: _Indexed,
) -> None:
    # The durable marker is the only evidence: nothing failed in this process.
    assert RunMarker(lambda: indexed.store).require(PROJECT) is None
    tools = indexed.tools()

    with pytest.raises(ValueError) as refused:
        tools._guarded_rename_reingest(PROJECT)

    assert str(refused.value) == mcs.MCP_REINGEST_AFTER_FAILED_RUN.format(
        project=PROJECT
    )


def test_a_reingest_whose_mark_fails_never_starts(indexed: _Indexed) -> None:
    tools = indexed.tools()
    updater = _Updater()
    tools._live_updater = updater
    callback = tools._guarded_rename_reingest(PROJECT)
    assert callback is not None
    indexed.store._failing.add("mark")

    with pytest.raises(RuntimeError) as refused:
        callback(["pkg/util.py"])

    assert str(refused.value) == mcs.MCP_INCOMPLETE_MARKER_REQUIRED.format(
        project=PROJECT
    )
    assert updater.called is False


def test_a_reingest_that_fails_before_writing_lifts_its_marker(
    indexed: _Indexed,
) -> None:
    tools = indexed.tools()
    updater = _Updater(fail=ReingestAborted("bad path"), writes=False)
    tools._live_updater = updater
    callback = tools._guarded_rename_reingest(PROJECT)
    assert callback is not None

    with pytest.raises(ReingestAborted):
        callback(["pkg/util.py"])

    assert not indexed.marked()
    assert tools._live_updater is updater
    assert not tools.marker.damage.blocks(PROJECT)


def test_a_reingest_that_fails_after_writing_keeps_its_marker(
    indexed: _Indexed,
) -> None:
    tools = indexed.tools()
    tools._live_updater = _Updater(fail=RuntimeError("lost"))
    callback = tools._guarded_rename_reingest(PROJECT)
    assert callback is not None

    with pytest.raises(RuntimeError, match="lost"):
        callback(["pkg/util.py"])

    assert indexed.events == ["mark", "writing"]
    assert indexed.marked()
    assert tools._live_updater is None
    assert tools.marker.damage.blocks(PROJECT)
