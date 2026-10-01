"""Startup failures must not look like successful CLI output."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    ("command", "target"),
    [
        (
            [
                "start",
                "--no-start-stack",
                "--no-sync",
                "-a",
                "question",
                "--output-format",
                "json",
            ],
            "_run_single_query",
        ),
        (["optimize", "python"], "main_optimize_async"),
    ],
)
def test_startup_failure_exits_nonzero_without_stdout(
    tmp_path: Path, command: list[str], target: str
) -> None:
    (tmp_path / ".git").mkdir()
    mock_type = AsyncMock if target == "main_optimize_async" else MagicMock
    with (
        patch("codebase_rag.cli._update_and_validate_models"),
        patch(
            f"codebase_rag.cli.{target}",
            new_callable=mock_type,
            side_effect=ValueError("model unavailable"),
        ),
    ):
        result = runner.invoke(app, [*command, "--repo-path", str(tmp_path)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Startup Error: model unavailable" in result.stderr
    assert "\x1b[" not in result.stderr


@pytest.mark.parametrize(
    ("command", "target"),
    [
        (
            ["start", "--no-start-stack", "--no-sync", "-a", "question"],
            "_run_single_query",
        ),
        (["optimize", "python"], "main_optimize_async"),
    ],
)
def test_interrupt_exits_130_without_stdout(
    tmp_path: Path, command: list[str], target: str
) -> None:
    (tmp_path / ".git").mkdir()
    mock_type = AsyncMock if target == "main_optimize_async" else MagicMock
    with (
        patch("codebase_rag.cli._update_and_validate_models"),
        patch(
            f"codebase_rag.cli.{target}",
            new_callable=mock_type,
            side_effect=KeyboardInterrupt,
        ),
    ):
        result = runner.invoke(app, [*command, "--repo-path", str(tmp_path)])

    assert result.exit_code == 130
    assert result.stdout == ""
    assert "Application terminated by user." in result.stderr
