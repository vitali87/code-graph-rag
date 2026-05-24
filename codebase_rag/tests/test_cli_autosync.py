from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app

runner = CliRunner()


@pytest.fixture
def mock_memgraph_connect() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


@pytest.fixture
def mock_agent_loops() -> Generator[None, None, None]:
    with (
        patch("codebase_rag.cli.main_async") as mock_async,
        patch("codebase_rag.cli.main_single_query") as mock_single,
        patch("codebase_rag.cli.asyncio.run"),
    ):
        mock_async.return_value = None
        mock_single.return_value = None
        yield


@pytest.fixture
def mock_sync_path() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli._run_graph_sync") as mock_sync:
        yield mock_sync


@pytest.fixture
def mock_validate_models() -> Generator[None, None, None]:
    with patch("codebase_rag.cli._update_and_validate_models"):
        yield


def test_start_default_triggers_auto_sync(
    mock_memgraph_connect: MagicMock,
    mock_agent_loops: None,
    mock_sync_path: MagicMock,
    mock_validate_models: None,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        ["start", "--repo-path", str(tmp_path), "--ask-agent", "hello"],
    )
    assert result.exit_code == 0, result.output
    mock_sync_path.assert_called_once()


def test_start_no_sync_skips_auto_sync(
    mock_memgraph_connect: MagicMock,
    mock_agent_loops: None,
    mock_sync_path: MagicMock,
    mock_validate_models: None,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        ["start", "--repo-path", str(tmp_path), "--no-sync", "--ask-agent", "hello"],
    )
    assert result.exit_code == 0, result.output
    mock_sync_path.assert_not_called()


def test_start_update_graph_uses_sync_helper(
    mock_memgraph_connect: MagicMock,
    mock_agent_loops: None,
    mock_sync_path: MagicMock,
    mock_validate_models: None,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        ["start", "--repo-path", str(tmp_path), "--update-graph"],
    )
    assert result.exit_code == 0, result.output
    mock_sync_path.assert_called_once()
    call = mock_sync_path.call_args
    assert call.kwargs["repo"] == tmp_path.resolve()
    assert call.kwargs["clean"] is False


def test_start_clean_without_update_graph_does_not_sync(
    mock_memgraph_connect: MagicMock,
    mock_sync_path: MagicMock,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        ["start", "--repo-path", str(tmp_path), "--clean"],
    )
    assert result.exit_code == 0, result.output
    mock_sync_path.assert_not_called()


def test_start_auto_sync_uses_derived_project_name_when_none_provided(
    mock_memgraph_connect: MagicMock,
    mock_agent_loops: None,
    mock_sync_path: MagicMock,
    mock_validate_models: None,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        ["start", "--repo-path", str(tmp_path), "--ask-agent", "hi"],
    )
    assert result.exit_code == 0, result.output
    call = mock_sync_path.call_args
    project_name = call.kwargs["project_name"]
    assert "__" in project_name
    assert len(project_name.rsplit("__", 1)[1]) == 8


def test_start_auto_sync_respects_explicit_project_name(
    mock_memgraph_connect: MagicMock,
    mock_agent_loops: None,
    mock_sync_path: MagicMock,
    mock_validate_models: None,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "start",
            "--repo-path",
            str(tmp_path),
            "--project-name",
            "my-project",
            "--ask-agent",
            "hi",
        ],
    )
    assert result.exit_code == 0, result.output
    call = mock_sync_path.call_args
    assert call.kwargs["project_name"] == "my-project"
