"""Issue #2391 follow-up: an explicit but empty `cgr stats` scope is an error.

`--workspace` naming a workspace with no repositories, or `-n ""`, left the
requested project list empty, and an empty list is what "no scope" looks
like, so the command reported the whole shared graph as if it were the
workspace's (review of PR 2436). Whole-graph totals are for a call with
neither option.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from typer.testing import CliRunner

from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.types_defs import ResultRow
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo, save_workspace

_ROWS: list[ResultRow] = [{"labels": ["Function"], "count": 7}]


def _ingestor() -> MagicMock:
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.list_projects.return_value = ["alpha", "beta"]
    mock.fetch_all.return_value = _ROWS
    return mock


def _run(args: list[str], mock: MagicMock) -> tuple[int, str]:
    with patch("codebase_rag.cli.connect_memgraph", return_value=mock):
        result = CliRunner().invoke(app, ["stats", *args])
    return result.exit_code, " ".join(click.unstyle(result.output).split())


def _whole_graph_queried(mock: MagicMock) -> bool:
    queried = {c.args[0] for c in mock.fetch_all.call_args_list}
    return bool(
        queried & {cq.CYPHER_STATS_NODE_COUNTS, cq.CYPHER_STATS_RELATIONSHIP_COUNTS}
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path)
    return tmp_path


def _workspace(home: Path, name: str, repos: list[WorkspaceRepo]) -> None:
    save_workspace(WorkspaceConfig(name=name, repos=repos), home=home)


def test_a_workspace_without_repositories_is_not_the_whole_graph(home: Path) -> None:
    _workspace(home, "empty", [])
    mock = _ingestor()

    code, out = _run(["--workspace", "empty"], mock)

    assert code == 1, out
    assert "empty" in out
    assert "no repositories" in out
    assert not _whole_graph_queried(mock)


@pytest.mark.parametrize(
    "names", [[""], ["  "], ["", " "]], ids=["empty", "blank", "both"]
)
def test_a_blank_project_name_is_not_the_whole_graph(names: list[str]) -> None:
    mock = _ingestor()

    code, out = _run([arg for name in names for arg in ("-n", name)], mock)

    assert code == 1, out
    assert "--project-name" in out
    assert not _whole_graph_queried(mock)


def test_no_scope_option_still_counts_the_whole_graph() -> None:
    # Negative.
    mock = _ingestor()

    code, out = _run([], mock)

    assert code == 0, out
    assert _whole_graph_queried(mock)


def test_a_blank_name_beside_a_real_scope_is_just_dropped(home: Path) -> None:
    # Negative: the scope is not empty, so the blank name changes nothing.
    _workspace(home, "shop", [WorkspaceRepo(path="/src/alpha", project_name="alpha")])
    mock = _ingestor()

    code, out = _run(["-n", "", "--workspace", "shop"], mock)

    assert code == 0, out
    assert not _whole_graph_queried(mock)
    assert {"project_names": ["alpha"]} in [
        c.args[1] if len(c.args) > 1 else c.kwargs.get("params")
        for c in mock.fetch_all.call_args_list
    ]
