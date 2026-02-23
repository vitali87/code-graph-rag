import re
import subprocess
import sys
from importlib.metadata import version as get_version
from pathlib import Path

import pytest

from codebase_rag import constants as cs

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def test_help_command_works() -> None:
    repo_root = Path(__file__).parent.parent.parent

    result = subprocess.run(
        [sys.executable, "-m", "codebase_rag.cli", "--help"],
        check=False,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, "NO_COLOR": "1"},
    )

    assert result.returncode == 0, f"Help command failed with: {result.stderr}"

    plain_stdout = _ANSI_RE.sub("", result.stdout)
    assert "Usage:" in plain_stdout or "usage:" in plain_stdout.lower()
    assert "--help" in plain_stdout


def test_import_cli_module() -> None:
    try:
        from codebase_rag import cli

        assert hasattr(cli, "app"), "CLI module missing app attribute"
    except ImportError as e:
        pytest.fail(f"Failed to import cli module: {e}")


def test_version_flag() -> None:
    repo_root = Path(__file__).parent.parent.parent

    for flag in ["--version", "-v"]:
        result = subprocess.run(
            [sys.executable, "-m", "codebase_rag.cli", flag],
            check=False,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"{flag} exited with code {result.returncode}: {result.stderr}"
        )
        expected = cs.CLI_MSG_VERSION.format(version=get_version("code-graph-rag"))
        assert result.stdout.strip() == expected, (
            f"{flag} output did not match expected format: {repr(result.stdout)}"
        )
        assert result.stderr == "", f"Unexpected stderr for {flag}: {result.stderr}"
