"""The MCP server scoped to a workspace (issue #1494, the remaining criterion).

Six of the issue's seven criteria were met by enforcement in code
(`scope_rows_to_project`); the seventh was plumbing: the server had no way
to load a workspace, so its notion of which projects exist was never
workspace-backed. The rule that bounds this layer, from the issue's own
status check: the workspace is an allow-list on top of the graph's project
check, never a second, weaker path to the same decision.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp import server as mcp_server
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceError, WorkspaceRepo

ALPHA = "alpha__1111"
BETA = "beta__2222"
GAMMA = "gamma__3333"  # in the workspace, never indexed


def _workspace(tmp_path: Path, *repos: tuple[str, str]) -> WorkspaceConfig:
    return WorkspaceConfig(
        name="ws",
        repos=[
            WorkspaceRepo(path=str(tmp_path / folder), project_name=name)
            for folder, name in repos
        ],
    )


def _registry(
    tmp_path: Path, workspace: WorkspaceConfig | None, root: str = "root"
) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.list_projects.return_value = [ALPHA, BETA, "other__9999"]
    ingestor.fetch_all.return_value = []
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(tmp_path / root),
            ingestor=ingestor,
            cypher_gen=MagicMock(),
            workspace=workspace,
        )
    return registry


@pytest.mark.anyio
async def test_list_projects_shows_the_workspaces_indexed_projects_only(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA), ("g", GAMMA))
    result = await _registry(tmp_path, ws).list_projects()
    # `other` is indexed but not served; `gamma` is served but not indexed.
    assert result == {"projects": [ALPHA, BETA], "count": 2}


@pytest.mark.anyio
async def test_without_a_workspace_list_projects_is_unchanged(tmp_path: Path) -> None:
    result = await _registry(tmp_path, None).list_projects()
    assert result["projects"] == [ALPHA, BETA, "other__9999"]


@pytest.mark.anyio
async def test_a_project_outside_the_workspace_is_refused_naming_the_workspace(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws)
    result = await registry.resolve("x", project="other__9999")
    assert result == {
        "error": cs.MCP_PROJECT_OUTSIDE_WORKSPACE.format(
            project="other__9999", workspace="ws", known=f"{ALPHA}, {BETA}"
        )
    }


@pytest.mark.anyio
async def test_a_workspace_project_that_is_not_indexed_is_still_unknown(
    tmp_path: Path,
) -> None:
    """The allow-list narrows; it never admits: `gamma` is in the workspace
    and not in the graph, and reads as unknown, not as served."""
    ws = _workspace(tmp_path, ("a", ALPHA), ("g", GAMMA))
    registry = _registry(tmp_path, ws)
    result = await registry.resolve("x", project=GAMMA)
    assert result["error"].startswith(f"Unknown project {GAMMA!r}")


@pytest.mark.anyio
async def test_the_default_project_is_the_repo_rooted_here_or_the_only_one(
    tmp_path: Path,
) -> None:
    (tmp_path / "b").mkdir()
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws, root="b")
    assert registry._workspace_default_project() == BETA
    only = _registry(tmp_path, _workspace(tmp_path, ("a", ALPHA)), root="elsewhere")
    assert only._workspace_default_project() == ALPHA


@pytest.mark.anyio
async def test_several_workspace_projects_and_no_root_match_refuse_a_bare_request(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws, root="elsewhere")
    result = await registry.resolve("x")
    assert result == {
        "error": cs.MCP_WORKSPACE_DEFAULT_AMBIGUOUS.format(
            workspace="ws", count=2, known=f"{ALPHA}, {BETA}"
        )
    }


def test_source_is_read_from_the_workspace_repos_own_root(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws, root="a")
    with patch("codebase_rag.mcp.tools.graph_query.source_root_for") as root_for:
        registry._source_root_for(BETA)
        registry._source_root_for("other__9999")
    assert root_for.call_args_list[0].args[2] == (tmp_path / "b").resolve()
    # A project the workspace does not name keeps the server's own root.
    assert root_for.call_args_list[1].args[2] == Path(str(tmp_path / "a"))


def test_the_server_loads_the_workspace_from_the_flag_or_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = WorkspaceConfig(name="ws")
    with patch.object(mcp_server, "load_workspace", return_value=ws) as load:
        assert mcp_server.get_workspace("ws") is ws
        load.assert_called_once_with("ws")
    monkeypatch.setenv(cs.MCPEnvVar.MCP_WORKSPACE, "from-env")
    with patch.object(mcp_server, "load_workspace", return_value=ws) as load:
        assert mcp_server.get_workspace(None) is ws
        load.assert_called_once_with("from-env")
    monkeypatch.delenv(cs.MCPEnvVar.MCP_WORKSPACE)
    assert mcp_server.get_workspace(None) is None


def test_a_missing_workspace_is_a_start_up_configuration_error() -> None:
    with (
        patch.object(mcp_server, "load_workspace", side_effect=WorkspaceError("no")),
        pytest.raises(ValueError, match="no"),
    ):
        mcp_server.get_workspace("absent")


def test_the_server_scopes_cypher_generation_to_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(tmp_path))
    with (
        patch.object(mcp_server, "setup_logging"),
        patch.object(mcp_server, "load_workspace", return_value=ws),
        patch.object(mcp_server, "MemgraphIngestor"),
        patch.object(mcp_server, "CypherGenerator") as cypher,
        patch.object(mcp_server, "create_mcp_tools_registry") as registry,
        patch.object(
            type(mcp_server.settings), "active_orchestrator_config", MagicMock()
        ),
        patch.object(type(mcp_server.settings), "active_cypher_config", MagicMock()),
    ):
        mcp_server.create_server("ws")
    cypher.assert_called_once_with(active_projects=[ALPHA, BETA])
    assert registry.call_args.kwargs["workspace"] is ws


def test_the_cli_passes_the_workspace_through_to_both_transports() -> None:
    from codebase_rag import cli

    with patch("codebase_rag.mcp.serve_stdio") as stdio:
        cli.mcp_server(
            transport=cs.MCPTransport.STDIO, host=None, port=None, workspace="ws"
        )
    assert stdio.call_args.kwargs == {"workspace": "ws"}
    with patch("codebase_rag.mcp.serve_http") as http:
        cli.mcp_server(transport=cs.MCPTransport.HTTP, host="h", port=1, workspace="ws")
    assert http.call_args.kwargs["workspace"] == "ws"


def test_the_environment_variable_is_named_for_the_launch_config() -> None:
    """An MCP client's launch config has an environment and no flags."""
    assert cs.MCPEnvVar.MCP_WORKSPACE == "MCP_WORKSPACE"


@pytest.mark.anyio
async def test_every_project_taking_tool_applies_the_workspace_allow_list(
    tmp_path: Path,
) -> None:
    """`query_code_graph`, `semantic_search` and `find_duplicate_code` check
    projects on their own rather than through `_graph_query`; the allow-list
    and default must reach them too (local review P1)."""
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws, root="elsewhere")
    registry._semantic_search_tool = MagicMock()
    registry._find_duplicates_tool = MagicMock()
    outside = cs.MCP_PROJECT_OUTSIDE_WORKSPACE.format(
        project="other__9999", workspace="ws", known=f"{ALPHA}, {BETA}"
    )
    ambiguous = cs.MCP_WORKSPACE_DEFAULT_AMBIGUOUS.format(
        workspace="ws", count=2, known=f"{ALPHA}, {BETA}"
    )
    graph = await registry.query_code_graph("q", project="other__9999")
    assert graph["error"] == outside
    assert graph["results"] == []
    bare = await registry.query_code_graph("q")
    assert bare["error"] == ambiguous
    assert await registry.semantic_search("q", project="other__9999") == outside
    assert await registry.semantic_search("q") == ambiguous
    assert await registry.find_duplicate_code(project="other__9999") == outside
    assert await registry.find_duplicate_code() == ambiguous
    registry._semantic_search_tool.function.assert_not_called()
    registry._find_duplicates_tool.function.assert_not_called()


@pytest.mark.anyio
async def test_delete_project_is_held_to_the_workspace(tmp_path: Path) -> None:
    """A workspace server may delete only what it serves (bot review on
    PR #1972); an indexed project outside the workspace is refused before
    any write."""
    ws = _workspace(tmp_path, ("a", ALPHA))
    registry = _registry(tmp_path, ws)
    result = await registry.delete_project("other__9999")
    assert result["success"] is False
    assert result["error"] == cs.MCP_PROJECT_OUTSIDE_WORKSPACE.format(
        project="other__9999", workspace="ws", known=ALPHA
    )
    registry.ingestor.delete_project.assert_not_called()


@pytest.mark.anyio
async def test_the_agents_tools_are_held_to_the_workspace(tmp_path: Path) -> None:
    """`ask_agent` builds its own tool list; under a workspace the graph
    query is bound to the workspace default and the project-taking tools
    refuse a project outside the allow-list (bot review on PR #1972)."""
    (tmp_path / "a").mkdir()
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws, root="a")
    registry._semantic_search_tool = MagicMock()
    registry._semantic_search_tool.function = MagicMock(return_value="ran")
    registry._semantic_search_tool.name = "semantic_search"
    registry._semantic_search_tool.description = "d"

    async def duplicates(project: str | None = None) -> str:
        return f"dups:{project}"

    # `name=` on a MagicMock is its repr, not an attribute: set it explicitly.
    dup_tool = MagicMock()
    dup_tool.function = duplicates
    dup_tool.name = "find_duplicate_code"
    dup_tool.description = "d"
    registry._find_duplicates_tool = dup_tool

    async def snippet(qualified_name: str) -> SimpleNamespace:
        return SimpleNamespace(
            model_dump=lambda: {"found": True, "qualified_name": qualified_name}
        )

    async def source(node_id: int) -> str:
        return f"source:{node_id}"

    code_tool = MagicMock()
    code_tool.function = snippet
    code_tool.name = "get_code_snippet"
    code_tool.description = "d"
    source_tool = MagicMock()
    source_tool.function = source
    source_tool.name = "get_function_source_by_id"
    source_tool.description = "d"
    # The registry wraps the real tools once at construction; the doubles
    # take the same wrap, since both routes call the wrapped instances.
    registry._code_tool = registry._workspace_scoped_by_name(code_tool)
    registry._function_source_tool = registry._workspace_scoped_by_node(source_tool)
    # Node 7 is defined in a served project, node 8 outside, node 9 nowhere.
    by_id = {7: f"{BETA}.pkg.f", 8: "other__9999.pkg.g"}
    registry.ingestor.fetch_all.side_effect = lambda query, params=None: (
        [{"qualified_name": by_id[params["node_id"]]}]
        if params and params.get("node_id") in by_id
        else []
    )
    with (
        patch("codebase_rag.mcp.tools.create_rag_orchestrator") as build,
        patch("codebase_rag.mcp.tools.create_query_tool") as query_tool,
    ):
        build.return_value = (MagicMock(), None)
        registry.rag_agent
    # The graph query is bound to the workspace default (the repo rooted here).
    assert query_tool.call_args.kwargs["project_name"] == ALPHA
    tools = {t.name: t for t in build.call_args.kwargs["tools"] if hasattr(t, "name")}
    dup = tools["find_duplicate_code"]
    assert await dup.function(project="other__9999") == (
        cs.MCP_PROJECT_OUTSIDE_WORKSPACE.format(
            project="other__9999", workspace="ws", known=f"{ALPHA}, {BETA}"
        )
    )
    # A bare call takes the workspace default; an allowed name passes through.
    assert await dup.function() == f"dups:{ALPHA}"
    assert await dup.function(project=BETA) == f"dups:{BETA}"
    # The source readers take a name or a node id, not a project: the name's
    # project prefix is what the allow-list is applied to.
    refused = cs.MCP_NAME_OUTSIDE_WORKSPACE.format(
        name="other__9999.pkg.g", workspace="ws", known=f"{ALPHA}, {BETA}"
    )
    code = tools["get_code_snippet"]
    assert await code.function("other__9999.pkg.g") == refused
    allowed = await code.function(f"{BETA}.pkg.f")
    assert allowed.model_dump()["qualified_name"] == f"{BETA}.pkg.f"
    by_node = tools["get_function_source_by_id"]
    assert await by_node.function(7) == "source:7"
    assert await by_node.function(8) == refused
    # An unknown id is the tool's to report, not the allow-list's.
    assert await by_node.function(9) == "source:9"
    # The direct MCP handlers share the guard (local review P1): the same
    # inputs are refused there too, before the graph is consulted.
    direct = await registry.get_code_snippet("other__9999.pkg.g")
    assert direct["error_message"] == refused
    assert direct["found"] is False
    assert await registry.get_function_source(8) == refused
    assert (await registry.get_code_snippet(f"{BETA}.pkg.f"))["found"] is True


@pytest.mark.anyio
async def test_the_direct_handlers_refuse_with_the_real_tools_wrapped_once(
    tmp_path: Path,
) -> None:
    """The guard is applied to the tool instances at construction, so the
    MCP handlers refuse without any double standing in: a name outside the
    workspace never reaches the retriever, a node id is resolved to its
    name first (local review P1 on PR #1972)."""
    (tmp_path / "a").mkdir()
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws, root="a")
    registry.ingestor.fetch_all.side_effect = lambda query, params=None: (
        [{"qualified_name": "other__9999.pkg.g"}]
        if params and params.get("node_id") == 8
        else []
    )
    refused = cs.MCP_NAME_OUTSIDE_WORKSPACE.format(
        name="other__9999.pkg.g", workspace="ws", known=f"{ALPHA}, {BETA}"
    )
    snippet = await registry.get_code_snippet("other__9999.pkg.g")
    assert snippet["error_message"] == refused
    assert snippet["found"] is False
    assert await registry.get_function_source(8) == refused


@pytest.mark.anyio
async def test_a_name_under_a_longer_indexed_project_is_refused(
    tmp_path: Path,
) -> None:
    """Workspace `foo` must not serve `foo.bar.pkg.fn` when `foo.bar` is an
    indexed project outside it: the name's project is the longest indexed
    name it sits under, not the first served prefix (bot review on PR
    #1972). A name under `foo` itself still passes."""
    (tmp_path / "a").mkdir()
    ws = _workspace(tmp_path, ("a", "foo"))
    registry = _registry(tmp_path, ws, root="a")
    registry.ingestor.list_projects.return_value = ["foo", "foo.bar"]
    refused = cs.MCP_NAME_OUTSIDE_WORKSPACE.format(
        name="foo.bar.pkg.fn", workspace="ws", known="foo"
    )
    assert registry._workspace_name_refusal("foo.bar.pkg.fn") == refused
    assert registry._workspace_name_refusal("foo.pkg.fn") is None
    assert registry._workspace_name_refusal("foo") is None
    snippet = await registry.get_code_snippet("foo.bar.pkg.fn")
    assert snippet["error_message"] == refused


def test_without_a_default_the_agents_graph_query_refuses(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, ("a", ALPHA), ("b", BETA))
    registry = _registry(tmp_path, ws, root="elsewhere")
    tool = registry._agent_query_tool()

    answer = tool.function("anything")
    assert answer == cs.MCP_WORKSPACE_DEFAULT_AMBIGUOUS.format(
        workspace="ws", count=2, known=f"{ALPHA}, {BETA}"
    )
    assert registry._agent_query_tool() is not registry._query_tool
    plain = _registry(tmp_path, None)
    assert plain._agent_query_tool() is plain._query_tool
