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
