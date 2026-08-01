# The --capture flag must reach the sync path for the normal start and
# ask-agent runs, not only the --update-graph path (PR #638 review).

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from codebase_rag.cli import app

runner = CliRunner()


def _invoke_ask(tmp_path: Path, *extra: str) -> MagicMock:
    with (
        patch("codebase_rag.cli._run_graph_sync") as mock_sync,
        patch("codebase_rag.cli.main_single_query"),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        result = runner.invoke(
            app,
            [
                "start",
                "--ask-agent",
                "hello",
                "--no-start-stack",
                "--repo-path",
                str(tmp_path),
                *extra,
            ],
        )
    assert result.exit_code == 0, result.output
    return mock_sync


def test_capture_flag_reaches_sync_on_ask_agent(tmp_path: Path) -> None:
    mock_sync = _invoke_ask(tmp_path, "--capture", "io")
    assert mock_sync.call_args.kwargs["capture"] == ["io"]


def test_capture_defaults_to_none_without_flag(tmp_path: Path) -> None:
    mock_sync = _invoke_ask(tmp_path)
    assert mock_sync.call_args.kwargs["capture"] is None
