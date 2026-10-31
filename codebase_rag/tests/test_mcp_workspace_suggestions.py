"""A workspace MCP server names only the projects it serves when it refuses one.

Follow-up to PR #2562 (issue #2461; CodeRabbit, CWE-200). The graph is shared
by every project indexed into it, and a workspace is the allow-list of the
projects this server serves. A served project that is not indexed passes the
allow-list and is refused as unknown, and that refusal's "did you mean" and
its fallback list were drawn from every project in the graph: they named
projects outside the workspace, which `list_projects` and the outside-the-
workspace refusal both keep from the client.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo

ALPHA = "alpha__11111111"
# Served by the workspaces below and never indexed: one transposition away
# from OUTSIDE, which the graph holds outside every workspace here.
SERVED = "gq__e7a07870"
OUTSIDE = "gq__e7a07807"
# Another checkout of `gq`: the same `<dir>` part as SERVED, so a close match.
TWIN = "gq__0badc0de"
OTHER = "other__99999999"
# Served, never indexed, and spelled close to nothing in the graph.
GAMMA = "gamma__33333333"
INDEXED: dict[str, str | None] = {
    ALPHA: "/repos/alpha",
    OUTSIDE: "/repos/gq",
    TWIN: "/elsewhere/gq",
    OTHER: "/repos/other",
}
NOT_SERVED = (OUTSIDE, TWIN, OTHER)
WS = "ws"

TOOLS = ["resolve", "callers", "query_code_graph", "semantic_search"]


def _workspace(root: Path, *projects: str) -> WorkspaceConfig:
    return WorkspaceConfig(
        name=WS,
        repos=[
            WorkspaceRepo(path=str(root / name), project_name=name) for name in projects
        ],
    )


def _registry(
    root: Path,
    workspace: WorkspaceConfig | None,
    roots: dict[str, str | None] | None = None,
) -> MCPToolsRegistry:
    graph = dict(INDEXED if roots is None else roots)
    ingestor = MagicMock()
    ingestor.list_projects.side_effect = lambda: sorted(graph)
    ingestor.list_project_roots.side_effect = lambda: dict(graph)
    ingestor.fetch_all.return_value = []
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(root),
            ingestor=ingestor,
            cypher_gen=MagicMock(),
            workspace=workspace,
        )
    # The refusal comes before the search runs; only the handler's guard
    # that semantic search is installed needs satisfying.
    registry._semantic_search_tool = MagicMock()
    return registry


async def _refusal(registry: MCPToolsRegistry, tool: str, project: str) -> str:
    """The refusal text `tool` answers `project` with, whatever its shape."""
    if tool == "query_code_graph":
        result = await registry.query_code_graph("what calls total", project=project)
        return str(result.get(cs.DICT_KEY_ERROR))
    if tool == "semantic_search":
        return await registry.semantic_search("totals", project=project)
    handler = getattr(registry, tool)
    result = await handler(f"{project}.app.total", project=project)
    assert isinstance(result, dict)
    return str(result[cs.DICT_KEY_ERROR])


def _rooted_here_and_spelled_close(
    root: Path, roots: dict[str, str | None]
) -> tuple[str, str]:
    """(the name `root` derives to, an indexed name spelled close to it), with
    `custom` indexed from `root` itself; neither is the derived name, so the
    root reads as never indexed."""
    derived = derive_project_name(root)
    near = f"{derived[:-1]}{'1' if derived.endswith('0') else '0'}"
    roots["custom"] = str(root)
    roots[near] = "/elsewhere/near"
    return derived, near


class TestUnderAWorkspace:
    @pytest.mark.parametrize("tool", TOOLS)
    async def test_the_closest_spelling_outside_the_workspace_is_not_suggested(
        self, tmp_path: Path, tool: str
    ) -> None:
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED, ALPHA))

        refusal = await _refusal(registry, tool, SERVED)

        assert refusal.startswith(f"Unknown project {SERVED!r}")
        assert [name for name in NOT_SERVED if name in refusal] == []
        # The workspace's indexed project is the lead left to name.
        assert ALPHA in refusal

    @pytest.mark.parametrize("tool", TOOLS)
    async def test_the_fallback_list_names_only_the_workspaces_projects(
        self, tmp_path: Path, tool: str
    ) -> None:
        registry = _registry(tmp_path, _workspace(tmp_path, GAMMA, ALPHA))

        refusal = await _refusal(registry, tool, GAMMA)

        assert refusal == cs.MCP_UNKNOWN_WORKSPACE_PROJECT.format(
            project=GAMMA, workspace=WS, known=ALPHA
        )

    @pytest.mark.parametrize("tool", TOOLS)
    async def test_a_workspace_with_nothing_indexed_names_no_project(
        self, tmp_path: Path, tool: str
    ) -> None:
        # The empty allow-listed case: no "Indexed projects: " with nothing
        # after it, and nothing from outside to fill the gap.
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED, GAMMA))

        refusal = await _refusal(registry, tool, SERVED)

        assert refusal == cs.MCP_UNKNOWN_WORKSPACE_PROJECT_NONE_INDEXED.format(
            project=SERVED, workspace=WS
        )

    def test_the_agents_query_tool_names_no_project_outside_the_workspace(
        self, tmp_path: Path
    ) -> None:
        # SERVED is the workspace's only project, so it is the default the
        # agent's tool binds to, and it is not indexed.
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED))

        refusal = str(registry._agent_query_tool().function("anything"))

        assert refusal == cs.MCP_UNKNOWN_WORKSPACE_PROJECT_NONE_INDEXED.format(
            project=SERVED, workspace=WS
        )

    def test_the_unindexed_root_refusal_names_no_project_outside_the_workspace(
        self, tmp_path: Path
    ) -> None:
        # Not reached through the tools today: under a workspace every
        # request has a served project, refused as unknown first. Held to
        # the same allow-list so no later caller reopens the disclosure.
        roots = dict(INDEXED)
        derived, _near = _rooted_here_and_spelled_close(tmp_path, roots)
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED), roots)

        refusal = registry._root_not_indexed_error(derived, sorted(roots))

        assert refusal == cs.MCP_ROOT_NOT_INDEXED.format(path=tmp_path.resolve())


class TestWhatStillHolds:
    async def test_a_close_match_inside_the_workspace_is_still_suggested(
        self, tmp_path: Path
    ) -> None:
        # Negative: TWIN is served and indexed; OUTSIDE is the closer
        # spelling, and not served.
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED, TWIN))

        refusal = await _refusal(registry, "resolve", SERVED)

        assert refusal == cs.MCP_UNKNOWN_PROJECT_NAMED.format(
            project=SERVED
        ) + graph_query.did_you_mean([TWIN])

    def test_a_workspace_project_rooted_here_is_still_suggested(
        self, tmp_path: Path
    ) -> None:
        # Negative: `custom` is indexed from the server's root and served.
        roots = dict(INDEXED)
        derived, near = _rooted_here_and_spelled_close(tmp_path, roots)
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED, "custom"), roots)

        refusal = registry._root_not_indexed_error(derived, sorted(roots))

        assert refusal.endswith(graph_query.did_you_mean(["custom"]))
        assert near not in refusal

    async def test_a_served_indexed_project_still_answers(self, tmp_path: Path) -> None:
        # Negative: the allow-list filters the refusal's names, not the reads.
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED, ALPHA))

        assert await registry.resolve("total", project=ALPHA) == []

    async def test_a_project_outside_the_workspace_is_refused_as_before(
        self, tmp_path: Path
    ) -> None:
        # Negative: refused by the allow-list, naming what it serves.
        registry = _registry(tmp_path, _workspace(tmp_path, SERVED, ALPHA))

        assert await registry.resolve("total", project=OUTSIDE) == {
            cs.DICT_KEY_ERROR: cs.MCP_PROJECT_OUTSIDE_WORKSPACE.format(
                project=OUTSIDE, workspace=WS, known=f"{SERVED}, {ALPHA}"
            )
        }


class TestWithoutAWorkspace:
    """Negative: every indexed project is this server's to name."""

    @pytest.mark.parametrize("tool", TOOLS)
    async def test_the_closest_spelling_is_still_suggested(
        self, tmp_path: Path, tool: str
    ) -> None:
        registry = _registry(tmp_path, None)

        refusal = await _refusal(registry, tool, SERVED)

        assert refusal == cs.MCP_UNKNOWN_PROJECT_NAMED.format(
            project=SERVED
        ) + graph_query.did_you_mean([OUTSIDE, TWIN])

    @pytest.mark.parametrize("tool", TOOLS)
    async def test_every_indexed_project_is_still_listed(
        self, tmp_path: Path, tool: str
    ) -> None:
        registry = _registry(tmp_path, None)

        refusal = await _refusal(registry, tool, GAMMA)

        assert refusal == cs.MCP_UNKNOWN_PROJECT.format(
            project=GAMMA, known=cs.SEPARATOR_COMMA_SPACE.join(sorted(INDEXED))
        )

    def test_the_unindexed_root_refusal_still_names_every_lead(
        self, tmp_path: Path
    ) -> None:
        roots = dict(INDEXED)
        derived, near = _rooted_here_and_spelled_close(tmp_path, roots)
        registry = _registry(tmp_path, None, roots)

        refusal = registry._root_not_indexed_error(derived, sorted(roots))

        assert refusal == cs.MCP_ROOT_NOT_INDEXED.format(
            path=tmp_path.resolve()
        ) + graph_query.did_you_mean(["custom", near])

    async def test_an_empty_graph_still_lists_nothing(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, None, roots={})

        refusal = await _refusal(registry, "resolve", GAMMA)

        assert refusal == cs.MCP_UNKNOWN_PROJECT.format(project=GAMMA, known="")
