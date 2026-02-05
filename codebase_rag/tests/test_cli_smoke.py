import subprocess
import sys
from pathlib import Path

import pytest


def test_help_command_works() -> None:
    repo_root = Path(__file__).parent.parent.parent

    result = subprocess.run(
        [sys.executable, "-m", "codebase_rag.cli", "--help"],
        check=False,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=45,
    )

    assert result.returncode == 0, f"Help command failed with: {result.stderr}"

    # (H) Remove ANSI escape codes for robust testing
    import re

    stdout_clean = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)

    assert "Usage:" in stdout_clean or "usage:" in stdout_clean.lower()
    assert "--help" in stdout_clean or "help" in stdout_clean.lower()

    assert result.stderr == "", f"Unexpected stderr: {result.stderr}"


def test_import_cli_module() -> None:
    try:
        from codebase_rag import cli

        assert hasattr(cli, "app"), "CLI module missing app attribute"
    except ImportError as e:
        pytest.fail(f"Failed to import cli module: {e}")
