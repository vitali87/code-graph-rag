"""The context slice reads source only from the selected project's own root
(issue #1536).

Both entry points accept a project other than the checkout they run in. A
definition of that project carries a repo-relative path that may also exist
in the local checkout, so reading it there would return local source labelled
as the selected project's. The slice must take its source root from the
Project node's stored root, the way `definition` does, and answer a foreign
project with no excerpt rather than the wrong one.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from evals.cgr_graph import _StatefulIngestor

LOCAL = "local"
OTHER = "other"
REL = "pkg/mod.py"
LOCAL_BODY = "def f():\n    return 'local checkout source'\n"


def _function(store: _StatefulIngestor, project: str) -> str:
    qn = f"{project}.pkg.mod.f"
    store.ensure_node_batch(
        cs.NodeLabel.FUNCTION.value,
        {
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_NAME: "f",
            cs.KEY_PATH: REL,
            cs.KEY_START_LINE: 1,
            cs.KEY_END_LINE: 2,
        },
    )
    return qn


@pytest.fixture
def graph(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    local_root = temp_repo / LOCAL
    (local_root / "pkg").mkdir(parents=True)
    (local_root / REL).write_text(LOCAL_BODY)
    store = _StatefulIngestor()
    for name, root in ((LOCAL, local_root), (OTHER, temp_repo / OTHER)):
        store.ensure_node_batch(
            cs.NodeLabel.PROJECT.value,
            {cs.KEY_NAME: name, cs.KEY_ROOT_PATH: str(root.resolve())},
        )
        _function(store, name)
    return local_root, store


def _sources(payload: object) -> str:
    assert isinstance(payload, dict)
    return "\n".join(piece["source"] for piece in payload["pieces"])


async def test_mcp_context_reads_no_local_source_for_another_project(
    graph: tuple[Path, _StatefulIngestor],
) -> None:
    from codebase_rag.mcp.tools import MCPToolsRegistry

    local_root, store = graph
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [LOCAL, OTHER]
    registry = MCPToolsRegistry(
        project_root=str(local_root), ingestor=ingestor, cypher_gen=MagicMock()
    )

    foreign = await registry.context(
        target=f"{OTHER}.pkg.mod.f", budget_tokens=500, project=OTHER
    )
    assert isinstance(foreign, dict) and foreign["resolved"] == f"{OTHER}.pkg.mod.f"
    assert "local checkout source" not in _sources(foreign)

    # The project indexed from this checkout still gets its excerpt.
    own = await registry.context(
        target=f"{LOCAL}.pkg.mod.f", budget_tokens=500, project=LOCAL
    )
    assert "local checkout source" in _sources(own)


def test_cli_context_reads_no_local_source_for_another_project(
    graph: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag import graph_cli
    from codebase_rag.cli import app

    local_root, store = graph

    def fake_project_and_fetch(project: str | None, repo_path: Path) -> tuple:
        return project or LOCAL, store.fetch_all, contextlib.nullcontext()

    monkeypatch.setattr(graph_cli, "_project_and_fetch", fake_project_and_fetch)

    def run(project: str) -> str:
        result = CliRunner().invoke(
            app,
            [
                "context",
                f"{project}.pkg.mod.f",
                "--repo-path",
                str(local_root),
                "--project",
                project,
            ],
        )
        assert result.exit_code == 0, result.output
        return _sources(json.loads(result.stdout))

    assert "local checkout source" not in run(OTHER)
    assert "local checkout source" in run(LOCAL)


def test_cli_context_exits_nonzero_when_nothing_matches(
    graph: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag import graph_cli
    from codebase_rag.cli import app

    local_root, store = graph
    monkeypatch.setattr(
        graph_cli,
        "_project_and_fetch",
        lambda project, repo_path: (LOCAL, store.fetch_all, contextlib.nullcontext()),
    )
    result = CliRunner().invoke(
        app, ["context", f"{LOCAL}.pkg.mod.missing", "--repo-path", str(local_root)]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["resolved"] is None
    assert cs.CONTEXT_UNRESOLVED.format(target=f"{LOCAL}.pkg.mod.missing") in (
        result.stderr
    )


async def test_mcp_context_scopes_free_text_search_to_the_resolved_project(
    graph: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    # With `project` omitted the tool reads the project this server's root
    # derives to; the embedding search that resolves free text must be held
    # to that same project, or it can pick another project's symbol that the
    # project-scoped reads then cannot find.
    from codebase_rag.mcp import tools as mcp_tools
    from codebase_rag.mcp.tools import MCPToolsRegistry
    from codebase_rag.utils.path_utils import derive_project_name

    local_root, store = graph
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [LOCAL, OTHER]
    registry = MCPToolsRegistry(
        project_root=str(local_root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._semantic_search_tool = MagicMock()
    scopes: list[str | None] = []

    def fake_search(
        _ingestor: object, _text: str, top_k: int = 5, project: str | None = None
    ) -> list:
        scopes.append(project)
        return [
            {
                "node_id": 1,
                "qualified_name": f"{project}.pkg.mod.f",
                "name": "f",
                "type": "Function",
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(mcp_tools, "semantic_code_search", fake_search)

    derived = derive_project_name(local_root)
    payload = await registry.context(target="return the checkout source")
    assert scopes == [derived]
    assert isinstance(payload, dict)
    assert payload["resolved"] == f"{derived}.pkg.mod.f"
