"""Quiet CLI logging must not contaminate command output."""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import cli


def test_quiet_error_log_stays_on_stderr() -> None:
    repo_root = Path(__file__).parents[2]
    probe = (
        "from types import SimpleNamespace\n"
        "from codebase_rag.cli import _global_options\n"
        "from loguru import logger\n"
        "_global_options(\n"
        "    SimpleNamespace(invoked_subcommand=None), quiet=True, version=None\n"
        ")\n"
        "logger.error('quiet-route-marker')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )

    assert "quiet-route-marker" in result.stderr
    assert result.stdout == ""


@pytest.fixture
def _restored_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(cli.settings, "QUIET", cli.settings.QUIET)
    yield
    # Quiet mode adds a sink on the runner's stderr, which closes with it.
    logger.remove()


@pytest.mark.usefixtures("_restored_logging")
@pytest.mark.parametrize(("value", "quiet"), [("1", True), ("false", False)])
def test_cgr_quiet_sets_quiet_mode_without_the_flag(value: str, quiet: bool) -> None:
    # The root callback assigned the flag's default False over the setting
    # CGR_QUIET had made True, so the variable never took effect.
    result = CliRunner().invoke(cli.app, ["help"], env={"CGR_QUIET": value})
    assert result.exit_code == 0
    assert cli.settings.QUIET is quiet
