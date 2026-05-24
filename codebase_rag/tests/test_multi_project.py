from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import _resolve_active_projects, app
from codebase_rag.prompts import build_rag_orchestrator_prompt

runner = CliRunner()


class TestResolveActiveProjects:
    def test_returns_default_when_no_projects_flag(self) -> None:
        assert _resolve_active_projects(None, "default_proj") == ["default_proj"]

    def test_returns_default_for_empty_string(self) -> None:
        assert _resolve_active_projects("", "default_proj") == ["default_proj"]

    def test_single_project_in_flag(self) -> None:
        assert _resolve_active_projects("only_one", "default_proj") == ["only_one"]

    def test_multiple_projects_comma_separated(self) -> None:
        assert _resolve_active_projects("a,b,c", "default_proj") == ["a", "b", "c"]

    def test_strips_whitespace(self) -> None:
        assert _resolve_active_projects(" a , b ,c ", "default_proj") == ["a", "b", "c"]

    def test_drops_empty_entries(self) -> None:
        assert _resolve_active_projects("a,,b,", "default_proj") == ["a", "b"]

    def test_all_empty_falls_back_to_default(self) -> None:
        assert _resolve_active_projects(",,", "default_proj") == ["default_proj"]


class TestPromptActiveProjectsBlock:
    def test_no_projects_lists_list_projects_hint(self) -> None:
        prompt = build_rag_orchestrator_prompt([], active_projects=None)
        assert "list_projects" in prompt
        assert "Project Scope" in prompt

    def test_single_project_mentions_starts_with(self) -> None:
        prompt = build_rag_orchestrator_prompt([], active_projects=["only_one"])
        assert "only_one" in prompt
        assert "STARTS WITH" in prompt

    def test_multiple_projects_lists_all(self) -> None:
        prompt = build_rag_orchestrator_prompt([], active_projects=["a", "b", "c"])
        for name in ["a", "b", "c"]:
            assert f"`{name}`" in prompt or f"'{name}." in prompt
        assert "STARTS WITH 'a.'" in prompt
        assert "STARTS WITH 'b.'" in prompt


@pytest.fixture
def mock_memgraph_connect() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


@pytest.fixture
def mock_sync_path() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli._run_graph_sync"):
        yield


@pytest.fixture
def mock_validate_models() -> Generator[None, None, None]:
    with patch("codebase_rag.cli._update_and_validate_models"):
        yield


def test_start_passes_projects_to_single_query(
    mock_memgraph_connect: MagicMock,
    mock_sync_path: None,
    mock_validate_models: None,
    tmp_path: Path,
) -> None:
    with patch("codebase_rag.cli.main_single_query") as mock_single:
        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(tmp_path),
                "--projects",
                "alpha,beta",
                "--ask-agent",
                "hi",
                "--no-sync",
            ],
        )
        assert result.exit_code == 0, result.output
    mock_single.assert_called_once()
    assert mock_single.call_args.kwargs["active_projects"] == ["alpha", "beta"]


def test_start_default_projects_uses_derived_name(
    mock_memgraph_connect: MagicMock,
    mock_sync_path: None,
    mock_validate_models: None,
    tmp_path: Path,
) -> None:
    with patch("codebase_rag.cli.main_single_query") as mock_single:
        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(tmp_path),
                "--ask-agent",
                "hi",
                "--no-sync",
            ],
        )
        assert result.exit_code == 0, result.output
    mock_single.assert_called_once()
    active = mock_single.call_args.kwargs["active_projects"]
    assert len(active) == 1
    assert "__" in active[0]
