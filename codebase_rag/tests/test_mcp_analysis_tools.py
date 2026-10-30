"""`AnalysisTools`, the flow-verdict and traceback MCP handlers (issue #227),
driven on the mixin itself over a scripted graph. Pinned directly: the
refusals each makes before it walks anything (a root no project is indexed
for, #3193; a workspace with no default; a partial graph; a flow endpoint
that is no node, #2939, or a source of another project), the verdict's
remote hops (#1603), and the sections a report is laid out in: the
resolution rate a NamedTuple property would drop, and an ExceptionGroup's
members, nested (#3235)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.crash_correlation import (
    CYPHER_CRASH_CALLS,
    FrameContext,
    FrameResolutionRate,
    RootCause,
    RootCauseReport,
    TracebackReport,
)
from codebase_rag.cypher_queries import (
    CYPHER_GRAPH_NODE_EXISTS,
    CYPHER_LIST_PROJECTS,
    CYPHER_TRACE_CALLABLES,
)
from codebase_rag.flow_verdict import CYPHER_FLOW_EDGES, CYPHER_FLOW_REMOTE_EDGES
from codebase_rag.mcp import constants as mcs
from codebase_rag.mcp.analysis_tools import (
    AnalysisTools,
    _explained_section,
    _ranked_section,
)
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.tests.conftest import marker_store
from codebase_rag.types_defs import PropertyDict
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class _Analysis:
    """An `AnalysisTools` over a two-function service whose `dispatch`
    calls `handle`, and flows through `net` to `api.handle` in the project
    `<project>.v2`: another project, though its name extends this one's
    (#1970). Every graph read asked is recorded."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.project = derive_project_name(root)
        self.module = f"{self.project}.app.service"
        self.remote = f"{self.project}.v2.api.handle"
        self.asked: list[str] = []
        self.store = marker_store()
        self.store.list_projects.return_value = [self.project, f"{self.project}.v2"]
        read_marker = self.store.fetch_all.side_effect

        def fetch_all(query: str, params: PropertyDict | None = None) -> list:
            if "run_incomplete" in query:
                return read_marker(query, params)
            self.asked.append(query)
            return self._rows(query, params or {})

        self.store.fetch_all.side_effect = fetch_all
        tools = AnalysisTools()
        tools.project_root = str(root)
        tools.ingestor = self.store
        tools.workspace = None
        tools._ingestor_lock = asyncio.Lock()
        tools._live_updater = None
        tools.marker = RunMarker(lambda: tools.ingestor)
        self.tools = tools

    def _rows(self, query: str, params: PropertyDict) -> list[dict]:
        module = self.module
        if query == CYPHER_TRACE_CALLABLES:
            return [
                {
                    cs.KEY_LABEL: cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME: f"{module}.{name}",
                    cs.KEY_PATH: "app/service.py",
                    cs.KEY_START_LINE: start,
                    cs.KEY_END_LINE: start + 4,
                }
                for name, start in (("handle", 8), ("dispatch", 14))
            ]
        if query == CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: name} for name in self.store.list_projects()]
        if query == CYPHER_CRASH_CALLS:
            return [{"from_qn": f"{module}.dispatch", "to_qn": f"{module}.handle"}]
        if query == CYPHER_GRAPH_NODE_EXISTS:
            known = {f"{module}.{n}" for n in ("dispatch", "handle", "net")}
            known.add(self.remote)
            qn = params[cs.KEY_QN]
            return [{cs.KEY_QUALIFIED_NAME: qn}] if qn in known else []
        if query == CYPHER_FLOW_EDGES:
            return [{"source": f"{module}.dispatch", "target": f"{module}.net"}]
        if query == CYPHER_FLOW_REMOTE_EDGES:
            return [{"source": f"{module}.net", "target": self.remote}]
        return []

    def traceback(self) -> str:
        src = (self.root / "app" / "service.py").as_posix()
        return (
            "Traceback (most recent call last):\n"
            f'  File "{src}", line 16, in dispatch\n'
            "    return handle(cfg)\n"
            f'  File "{src}", line 10, in handle\n'
            "    return cfg.timeout\n"
            "AttributeError: 'NoneType' object has no attribute 'timeout'\n"
        )


@pytest.fixture
def analysis(tmp_path: Path) -> _Analysis:
    return _Analysis(tmp_path)


Handler = Callable[[_Analysis], Awaitable[object]]
HANDLERS: list[tuple[cs.MCPToolName, Handler]] = [
    (
        cs.MCPToolName.FLOW_VERDICT,
        lambda a: a.tools.flow_verdict(f"{a.module}.dispatch", a.remote),
    ),
    (
        cs.MCPToolName.EXPLAIN_TRACEBACK,
        lambda a: a.tools.explain_traceback(a.traceback()),
    ),
    (
        cs.MCPToolName.RANK_ROOT_CAUSES,
        lambda a: a.tools.rank_root_causes(a.traceback()),
    ),
]
HANDLER_IDS = [str(tool) for tool, _ in HANDLERS]


# --- refusals before any walk -----------------------------------------------------


@pytest.mark.parametrize(("tool", "handler"), HANDLERS, ids=HANDLER_IDS)
async def test_a_root_no_project_is_indexed_for_is_refused(
    analysis: _Analysis, tool: cs.MCPToolName, handler: Handler
) -> None:
    # The verified absence of a flow, or a traceback resolving nothing, would
    # otherwise be answered for a graph that holds no project at all.
    analysis.store.list_projects.return_value = []

    result = await handler(analysis)

    assert isinstance(result, dict)
    assert result["error"].startswith(
        mcs.MCP_FIXED_ROOT_NOT_INDEXED.format(path=analysis.root.resolve())
    )
    assert analysis.asked == []


@pytest.mark.parametrize(("tool", "handler"), HANDLERS, ids=HANDLER_IDS)
async def test_a_workspace_with_no_default_project_is_refused(
    analysis: _Analysis, tmp_path: Path, tool: cs.MCPToolName, handler: Handler
) -> None:
    # Two repositories and neither at this root: the handlers take no
    # `project`, so there is no way to say which one is meant.
    analysis.tools.workspace = WorkspaceConfig(
        name="ws",
        repos=[
            WorkspaceRepo(path=str(tmp_path / "a"), project_name="a"),
            WorkspaceRepo(path=str(tmp_path / "b"), project_name="b"),
        ],
    )

    result = await handler(analysis)

    assert result == {
        "error": mcs.MCP_WORKSPACE_DEFAULT_AMBIGUOUS.format(
            workspace="ws", count=2, known="a, b"
        )
    }
    analysis.store.list_projects.assert_not_called()


@pytest.mark.parametrize(("tool", "handler"), HANDLERS, ids=HANDLER_IDS)
async def test_a_partial_graph_is_refused(
    analysis: _Analysis, tool: cs.MCPToolName, handler: Handler
) -> None:
    analysis.tools.marker.invalidate(analysis.project)

    result = await handler(analysis)

    assert result == {
        "error": mcs.MCP_QUERY_AFTER_FAILED_RUN.format(
            project=analysis.project, tool=tool
        )
    }
    assert analysis.asked == []


async def test_a_flow_endpoint_that_is_no_node_is_refused_by_side(
    analysis: _Analysis,
) -> None:
    missing_sink = f"{analysis.module}.nowhere"

    result = await analysis.tools.flow_verdict(
        f"{analysis.module}.dispatch", missing_sink
    )

    assert isinstance(result, dict)
    assert result["error"].startswith(
        mcs.MCP_FLOW_ENDPOINT.format(
            side=mcs.MCP_FLOW_SINK,
            problem=mcs.MCP_UNKNOWN_TARGET.format(qualified_name=missing_sink),
        )
    )
    assert f"The {mcs.MCP_FLOW_SOURCE}: " not in result["error"]
    assert CYPHER_FLOW_EDGES not in analysis.asked


async def test_both_unknown_flow_endpoints_are_named(analysis: _Analysis) -> None:
    result = await analysis.tools.flow_verdict("x.source", "x.sink")

    assert isinstance(result, dict)
    assert f"The {mcs.MCP_FLOW_SOURCE}: " in result["error"]
    assert f"The {mcs.MCP_FLOW_SINK}: " in result["error"]


async def test_a_source_another_project_owns_is_refused(analysis: _Analysis) -> None:
    # Only this project's flow edges are walked from the source; a sink may
    # sit across a service boundary, a source may not.
    result = await analysis.tools.flow_verdict(
        analysis.remote, f"{analysis.module}.handle"
    )

    assert result == {
        "error": mcs.MCP_FLOW_SOURCE_ELSEWHERE.format(
            qualified_name=analysis.remote,
            owner=f"{analysis.project}.v2",
            project=analysis.project,
        )
    }
    assert CYPHER_FLOW_EDGES not in analysis.asked


# --- answers ----------------------------------------------------------------------


async def test_a_flow_across_a_service_boundary_reports_its_hop(
    analysis: _Analysis,
) -> None:
    result = await analysis.tools.flow_verdict(
        f"{analysis.module}.dispatch", analysis.remote
    )

    assert result == {
        "verdict": "FOUND",
        "path": [
            f"{analysis.module}.dispatch",
            f"{analysis.module}.net",
            analysis.remote,
        ],
        "gaps": [],
        "remote_hops": [[f"{analysis.module}.net", analysis.remote]],
    }


async def test_an_explained_traceback_resolves_its_frames(analysis: _Analysis) -> None:
    result = await analysis.tools.explain_traceback(analysis.traceback())

    assert isinstance(result, dict)
    assert result["exception_type"] == "AttributeError"
    assert [frame["qualified_name"] for frame in result["frames"]] == [
        f"{analysis.module}.dispatch",
        f"{analysis.module}.handle",
    ]
    assert result["resolution"] == {"total": 2, "resolved": 2, "rate": 1.0}
    assert result["members"] == []


async def test_a_ranking_puts_the_caller_of_the_failing_frame_first(
    analysis: _Analysis,
) -> None:
    result = await analysis.tools.rank_root_causes(analysis.traceback())

    assert isinstance(result, dict)
    assert result["failing"] == f"{analysis.module}.handle"
    assert result["anchor_is_crash_site"] is True
    assert result["resolution"] == {"total": 2, "resolved": 2, "rate": 1.0}
    assert result["candidates"][0]["call_path"] == (
        f"{analysis.module}.dispatch",
        f"{analysis.module}.handle",
    )


# --- the sections a report is laid out in -----------------------------------------

FRAME = FrameContext(
    path="app/x.py",
    line=3,
    name="f",
    qualified_name="p.app.x.f",
    label="Function",
    unresolved_reason=None,
    callers=("p.app.x.g",),
    callees=(),
    flow_sources=(),
)


def test_an_exception_groups_members_are_explained_nested() -> None:
    inner = TracebackReport(
        exception_type="KeyError",
        exception_message="'k'",
        frames=(FRAME,),
        flow_gaps=(),
        resolution=FrameResolutionRate(total=5, resolved=2),
    )
    middle = inner._replace(
        exception_type="ExceptionGroup", members=(inner,), omitted_members=3
    )
    group = middle._replace(members=(middle,), frames=())

    section = _explained_section(group)

    # `rate` is a property, which `_asdict()` would leave out.
    assert section["resolution"] == {"total": 5, "resolved": 2, "rate": 0.4}
    assert section["frames"] == []
    (child,) = section["members"]
    assert child["omitted_members"] == 3
    (leaf,) = child["members"]
    assert leaf["exception_type"] == "KeyError"
    assert leaf["frames"] == [dict(FRAME._asdict())]
    assert leaf["members"] == []


def test_a_ranked_groups_members_are_ranked_nested() -> None:
    cause = RootCause(
        qualified_name="p.app.x.g",
        path="app/x.py",
        line=9,
        score=0.75,
        reasons=("calls the failing frame",),
        call_path=("p.app.x.g", "p.app.x.f"),
    )
    member = RootCauseReport(
        exception_type="KeyError",
        exception_message="'k'",
        failing="p.app.x.f",
        anchor_is_crash_site=True,
        candidates=(cause,),
        flow_used=False,
        flow_gaps=(),
        resolution=FrameResolutionRate(total=3, resolved=1),
    )
    group = member._replace(
        exception_type="ExceptionGroup",
        failing=None,
        candidates=(),
        members=(member,),
        resolution=FrameResolutionRate(total=0, resolved=0),
    )

    section = _ranked_section(group)

    assert section["resolution"] == {"total": 0, "resolved": 0, "rate": 0.0}
    (ranked,) = section["members"]
    assert ranked["candidates"] == [dict(cause._asdict())]
    assert ranked["resolution"]["rate"] == pytest.approx(1 / 3)
