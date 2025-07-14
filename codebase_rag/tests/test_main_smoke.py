"""Smoke tests for main CLI functionality."""

import subprocess
import sys
from pathlib import Path

import pytest


def test_help_command_works() -> None:
    """Test that the help command can be invoked without errors.

    This is a critical smoke test to ensure the CLI doesn't have import
    or configuration issues that would prevent basic functionality.
    """
    # Get the path to the main module
    repo_root = Path(__file__).parent.parent.parent

    # Run the help command as a subprocess to test the actual CLI
    result = subprocess.run(
        [sys.executable, "-m", "codebase_rag.main", "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # The command should exit successfully
    assert result.returncode == 0, f"Help command failed with: {result.stderr}"

    # Output should contain expected help text
    assert "Usage:" in result.stdout or "usage:" in result.stdout.lower()
    assert "--help" in result.stdout

    # Should not have any error output for a simple help command
    assert result.stderr == "", f"Unexpected stderr: {result.stderr}"


def test_import_main_module() -> None:
    """Test that the main module can be imported without errors."""
    try:
        from codebase_rag import main

        # Basic check that the module loaded
        assert hasattr(main, "app") or hasattr(main, "main"), (
            "Main module missing expected attributes"
        )
    except ImportError as e:
        pytest.fail(f"Failed to import main module: {e}")
