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
        timeout=30,
    )

    assert result.returncode == 0, f"Help command failed with: {result.stderr}"

    assert "Usage:" in result.stdout or "usage:" in result.stdout.lower()
    assert "--help" in result.stdout

    assert result.stderr == "", f"Unexpected stderr: {result.stderr}"


def test_import_cli_module() -> None:
    try:
        from codebase_rag import cli

        assert hasattr(cli, "app"), "CLI module missing app attribute"
    except ImportError as e:
        pytest.fail(f"Failed to import cli module: {e}")
