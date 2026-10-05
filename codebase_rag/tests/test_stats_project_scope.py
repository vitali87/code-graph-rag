"""Issue #2391: `cgr stats` can be scoped to projects or a workspace.

The graph is shared by every indexed repository, and `cgr stats` only ever
summed all of them, with no option to ask about one project.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.types_defs import ResultRow
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo, save_workspace

NODES: list[ResultRow] = [{"labels": ["Function"], "count": 7}]
RELS: list[ResultRow] = [{"type": "CALLS", "count": 3}]
BREAKDOWN: list[ResultRow] = [
    {"project": "alpha", "nodes": 5, "relationships": 2},
    {"project": "beta", "nodes": 9, "relationships": 4},
]


def _ingestor(projects: list[str]) -> MagicMock:
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.list_projects.return_value = projects

    def fetch_all(query: str, params: dict | None = None) -> list[ResultRow]:
        return {
            cq.CYPHER_STATS_NODE_COUNTS: NODES,
            cq.CYPHER_STATS_RELATIONSHIP_COUNTS: RELS,
            cq.CYPHER_STATS_PROJECT_NODE_COUNTS: NODES,
            cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS: RELS,
            cq.CYPHER_STATS_PER_PROJECT: BREAKDOWN,
        }[query]

    mock.fetch_all.side_effect = fetch_all
    return mock


def _run(args: list[str], mock: MagicMock) -> str:
    with patch("codebase_rag.cli.connect_memgraph", return_value=mock):
        result = CliRunner().invoke(app, ["stats", *args])
    return f"{result.exit_code}\n{result.output}"


def _queries(mock: MagicMock) -> list[tuple[str, dict | None]]:
    return [
        (c.args[0], c.args[1] if len(c.args) > 1 else c.kwargs.get("params"))
        for c in mock.fetch_all.call_args_list
    ]


def test_one_project_is_counted_on_its_own() -> None:
    mock = _ingestor(["alpha", "beta"])

    out = _run(["-n", "alpha"], mock)

    assert out.startswith("0\n")
    assert _queries(mock) == [
        (cq.CYPHER_STATS_PROJECT_NODE_COUNTS, {"project_names": ["alpha"]}),
        (cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS, {"project_names": ["alpha"]}),
    ]
    assert "alpha" in out


def test_the_project_option_repeats_and_takes_the_long_name() -> None:
    mock = _ingestor(["alpha", "beta", "gamma"])

    out = _run(["--project-name", "alpha", "-n", "gamma"], mock)

    assert out.startswith("0\n")
    assert {"project_names": ["alpha", "gamma"]} in [p for _q, p in _queries(mock)]


def test_a_workspace_counts_its_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path)
    save_workspace(
        WorkspaceConfig(
            name="shop",
            repos=[
                WorkspaceRepo(path="/src/alpha", project_name="alpha"),
                WorkspaceRepo(path="/src/beta", project_name="beta"),
            ],
        ),
        home=tmp_path,
    )
    mock = _ingestor(["alpha", "beta", "gamma"])

    out = _run(["--workspace", "shop"], mock)

    assert out.startswith("0\n"), out
    assert {"project_names": ["alpha", "beta"]} in [p for _q, p in _queries(mock)]


def test_an_unknown_project_is_an_error_that_names_the_known_ones() -> None:
    mock = _ingestor(["alpha", "beta"])

    out = _run(["-n", "nope"], mock)

    assert out.startswith("1\n")
    assert "nope" in out
    assert "alpha" in out
    assert not [q for q, _p in _queries(mock) if q in _COUNT_QUERIES]


def test_an_unknown_workspace_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path)
    mock = _ingestor(["alpha"])

    out = _run(["--workspace", "missing"], mock)

    assert out.startswith("1\n")
    assert mock.fetch_all.call_count == 0


def test_unscoped_totals_over_several_projects_are_attributed() -> None:
    mock = _ingestor(["alpha", "beta"])

    out = _run([], mock)

    assert out.startswith("0\n")
    assert "alpha: 5 nodes / 2 relationships" in out
    assert "beta: 9 nodes / 4 relationships" in out


def test_unscoped_totals_of_one_project_stay_as_they_were() -> None:
    # Negative: the default keeps today's two queries and adds no breakdown
    # when there is nothing to attribute.
    mock = _ingestor(["alpha"])

    out = _run([], mock)

    assert out.startswith("0\n")
    assert [q for q, _p in _queries(mock)] == [
        cq.CYPHER_STATS_NODE_COUNTS,
        cq.CYPHER_STATS_RELATIONSHIP_COUNTS,
    ]
    assert "nodes /" not in out


_COUNT_QUERIES = {
    cq.CYPHER_STATS_NODE_COUNTS,
    cq.CYPHER_STATS_RELATIONSHIP_COUNTS,
    cq.CYPHER_STATS_PROJECT_NODE_COUNTS,
    cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS,
}
