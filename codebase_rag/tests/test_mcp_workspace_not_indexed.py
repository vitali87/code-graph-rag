"""Issue #2867: a workspace server's `list_projects` names the workspace
projects the graph does not hold, and why.

The workspace is an allow-list over the graph's projects, so a workspace
repo that was never indexed under its own project name was dropped without
a word. Indexing the directory that holds the repos (one project for all of
them) left every workspace project unindexed, and `list_projects` answered
`{"projects": [], "count": 0}` for a graph of 300,000 nodes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo

ALPHA = "alpha__1111"
BETA = "beta__2222"
GAMMA = "gamma__3333"
PARENT = "projects__0000"
# `cgr start --update-graph` indexes one directory and does not read
# `--workspace`, so the hint names each repo's own command (Greptile, PR
# #2964).
INDEX_COMMAND = "cgr start --repo-path {path} --project-name {name} --update-graph"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    # The hint prints resolved paths.
    return tmp_path.resolve()


def _registry(
    base: Path, roots: dict[str, Path], *repos: tuple[str, str]
) -> tuple[MCPToolsRegistry, MagicMock]:
    ingestor = MagicMock()
    ingestor.list_projects.return_value = list(roots)
    ingestor.list_project_roots.return_value = {
        name: str(path) for name, path in roots.items()
    }
    ingestor.fetch_all.return_value = []
    workspace = WorkspaceConfig(
        name="ws",
        repos=[
            WorkspaceRepo(path=str(base / folder), project_name=name)
            for folder, name in repos
        ],
    )
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(base),
            ingestor=ingestor,
            cypher_gen=MagicMock(),
            workspace=workspace,
        )
    return registry, ingestor


@pytest.mark.anyio
async def test_a_workspace_project_missing_from_the_graph_is_named(
    root: Path,
) -> None:
    registry, ingestor = _registry(
        root,
        {ALPHA: root / "a", BETA: root / "b"},
        ("a", ALPHA),
        ("b", BETA),
        ("g", GAMMA),
    )

    result = await registry.list_projects()

    assert result["projects"] == [ALPHA, BETA]
    assert result["count"] == 2
    assert result.get("not_indexed") == [GAMMA]
    assert f"Workspace 'ws' projects not in the graph: {GAMMA}." in str(
        result.get("hint")
    )
    hint = str(result.get("hint"))
    assert INDEX_COMMAND.format(path=(root / "g").as_posix(), name=GAMMA) in hint
    assert "--workspace" not in hint


@pytest.mark.anyio
async def test_repos_indexed_inside_their_parent_directory_say_so(
    root: Path,
) -> None:
    # The report: the directory holding the repos was indexed as one
    # project, so the graph has one Project node and the workspace none.
    registry, ingestor = _registry(root, {PARENT: root}, ("a", ALPHA), ("b", BETA))

    result = await registry.list_projects()

    assert result["projects"] == []
    assert result["count"] == 0
    assert result.get("not_indexed") == [ALPHA, BETA]
    hint = str(result.get("hint"))
    for folder in ("a", "b"):
        assert (
            f"{root / folder} is indexed inside project '{PARENT}' "
            f"(rooted at {root}), not as a project of its own."
        ) in hint


@pytest.mark.anyio
async def test_the_closest_enclosing_project_is_the_one_named(
    root: Path,
) -> None:
    registry, ingestor = _registry(
        root,
        {PARENT: root, "nested__4444": root / "group"},
        ("group/a", ALPHA),
    )

    hint = str((await registry.list_projects()).get("hint"))

    assert "inside project 'nested__4444'" in hint
    assert PARENT not in hint


@pytest.mark.anyio
async def test_a_repo_indexed_under_another_name_says_which(
    root: Path,
) -> None:
    registry, ingestor = _registry(root, {"custom": root / "a"}, ("a", ALPHA))

    result = await registry.list_projects()

    assert result.get("not_indexed") == [ALPHA]
    assert (f"{root / 'a'} is indexed as project 'custom', not '{ALPHA}'.") in str(
        result.get("hint")
    )


# Negative: what must not change.


@pytest.mark.anyio
async def test_a_fully_indexed_workspace_lists_its_projects_only(
    root: Path,
) -> None:
    registry, ingestor = _registry(
        root,
        {ALPHA: root / "a", BETA: root / "b", "other__9999": root / "o"},
        ("a", ALPHA),
        ("b", BETA),
    )

    assert await registry.list_projects() == {"projects": [ALPHA, BETA], "count": 2}
    ingestor.list_project_roots.assert_not_called()


@pytest.mark.anyio
async def test_without_a_workspace_every_project_is_listed(root: Path) -> None:
    registry, ingestor = _registry(root, {ALPHA: root / "a"})
    registry.workspace = None

    assert await registry.list_projects() == {"projects": [ALPHA], "count": 1}
