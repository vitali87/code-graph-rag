"""Quiet CLI logging must not contaminate command output."""

import subprocess
import sys
from pathlib import Path


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
