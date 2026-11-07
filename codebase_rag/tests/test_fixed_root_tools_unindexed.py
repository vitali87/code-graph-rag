"""Issue #3193: the fixed-root MCP tools refuse a project the graph lacks.

flow_verdict, explain_traceback and rank_root_causes take no `project`: they
read the one this server's root derives to, and never asked the graph whether
it held it. An unindexed project has no FLOWS_TO edge and no Module to report
as uncovered, so flow_verdict answered NO_FLOW with no gaps, the verified
absence a taint question reads as safe, and the traceback tools resolved
nothing with `note: null`. The graph tools already refused the same root
(issue #2461); these now refuse it the same way.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.crash_correlation import CYPHER_CRASH_CALLS
from codebase_rag.flow_verdict import CYPHER_FLOW_COVERAGE_GAPS, CYPHER_FLOW_EDGES
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.types_defs import PropertyParams, ResultRow
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo

pytestmark = [pytest.mark.anyio]

OTHER = "tbapp"
ALPHA = "alpha__1111"
GAMMA = "gamma__3333"  # in the workspace, never indexed
# A resource no project owns, and a sink in another project: both pass the
# endpoint check (#2939), so only a project check stands between them and
# a verdict.
SOURCE = "ENV::K"
SINK = f"{OTHER}.app.leak"
# The reads an answer is built from. None may run for a project the graph
# does not hold.
ANSWER_QUERIES = {
    CYPHER_FLOW_EDGES,
    CYPHER_FLOW_COVERAGE_GAPS,
    cq.CYPHER_TRACE_CALLABLES,
    CYPHER_CRASH_CALLS,
}
TOOLS = [
    cs.MCPToolName.FLOW_VERDICT,
    cs.MCPToolName.EXPLAIN_TRACEBACK,
    cs.MCPToolName.RANK_ROOT_CAUSES,
]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _registry(
    root: Path,
    projects: dict[str, Path],
    workspace: WorkspaceConfig | None = None,
) -> tuple[MCPToolsRegistry, list[str]]:
    """A server rooted at `root` over a graph holding `projects` (name to
    root), and the list of every query it runs."""
    nodes = {SOURCE, SINK} if projects else set()
    ran: list[str] = []

    def fetch_all(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
        ran.append(query)
        if query == cq.CYPHER_GRAPH_NODE_EXISTS:
            qn = str((params or {})[cs.KEY_QN])
            return [{cs.KEY_QUALIFIED_NAME: qn}] if qn in nodes else []
        return []

    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=fetch_all)
    ingestor.list_projects.side_effect = lambda: sorted(projects)
    ingestor.list_project_roots.side_effect = lambda: {
        name: str(path) for name, path in projects.items()
    }
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(root),
            ingestor=ingestor,
            cypher_gen=MagicMock(),
            workspace=workspace,
        )
    return registry, ran


async def _ask(registry: MCPToolsRegistry, tool: cs.MCPToolName, root: Path) -> dict:
    if tool == cs.MCPToolName.FLOW_VERDICT:
        return await registry.flow_verdict(SOURCE, SINK)
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{(root / "app.py").as_posix()}", line 5, in leak\n'
        '    print(os.getenv("K"))\n'
        "ValueError: boom\n"
    )
    if tool == cs.MCPToolName.EXPLAIN_TRACEBACK:
        return await registry.explain_traceback(text)
    return await registry.rank_root_causes(text)


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_root_nothing_indexed_is_refused(
    tmp_path: Path, tool: cs.MCPToolName
) -> None:
    registry, ran = _registry(tmp_path, {})

    result = await _ask(registry, tool, tmp_path)

    assert set(result) == {cs.DICT_KEY_ERROR}
    error = result[cs.DICT_KEY_ERROR]
    assert str(tmp_path.resolve()) in error
    assert "index_repository" in error
    # These tools take no `project`, so they must not advise passing one.
    assert "`project`" not in error
    assert not ANSWER_QUERIES & set(ran)


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_repo_indexed_under_another_name_is_refused_naming_it(
    tmp_path: Path, tool: cs.MCPToolName
) -> None:
    # `cgr start --project-name tbapp`: the graph holds the repo, under a
    # name the server's `tbapp__<hash>` derivation never reaches.
    root = tmp_path / OTHER
    root.mkdir()
    registry, ran = _registry(root, {OTHER: root})

    result = await _ask(registry, tool, root)

    assert set(result) == {cs.DICT_KEY_ERROR}
    error = result[cs.DICT_KEY_ERROR]
    assert str(root.resolve()) in error
    assert f"Did you mean: {OTHER}" in error
    assert not ANSWER_QUERIES & set(ran)


@pytest.mark.parametrize("tool", TOOLS)
async def test_an_unindexed_workspace_default_is_refused(
    tmp_path: Path, tool: cs.MCPToolName
) -> None:
    # The workspace default is a named project, so the refusal is the one
    # `_graph_query` gives a named project the graph lacks.
    alpha, gamma = tmp_path / "a", tmp_path / "g"
    alpha.mkdir()
    gamma.mkdir()
    workspace = WorkspaceConfig(
        name="ws",
        repos=[
            WorkspaceRepo(path=str(alpha), project_name=ALPHA),
            WorkspaceRepo(path=str(gamma), project_name=GAMMA),
        ],
    )
    registry, ran = _registry(gamma, {ALPHA: alpha}, workspace)

    result = await _ask(registry, tool, gamma)

    assert set(result) == {cs.DICT_KEY_ERROR}
    assert f"Unknown project '{GAMMA}'" in result[cs.DICT_KEY_ERROR]
    assert not ANSWER_QUERIES & set(ran)


@pytest.mark.parametrize("tool", TOOLS)
async def test_an_indexed_root_still_answers(
    tmp_path: Path, tool: cs.MCPToolName
) -> None:
    """The control: green before and after the fix. The derived project is
    in the graph, so each tool reads it and answers rather than refusing;
    it proves the refusal above is about the project, not the fixture."""
    registry, ran = _registry(tmp_path, {derive_project_name(tmp_path): tmp_path})

    result = await _ask(registry, tool, tmp_path)

    assert cs.DICT_KEY_ERROR not in result
    assert ANSWER_QUERIES & set(ran)
