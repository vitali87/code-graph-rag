import re
import subprocess
import sys
from importlib.metadata import version as get_version
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag.cli import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# (H) Pin the render width: rich reads COLUMNS at console creation, and the
# (H) Windows CI runners report one narrow enough to reflow the Options panel
# (H) until asserted phrases split ("Delete every project from" failed there
# (H) while macOS/Ubuntu passed).
_RUNNER = CliRunner(env={"COLUMNS": "120"})


def _plain_stdout(result: Result) -> str:
    # (H) rich force-enables terminal styling under GITHUB_ACTIONS even though
    # (H) CliRunner's stream is no terminal, so on CI (and only there) escape
    # (H) codes split the asserted substrings ("Usage: \x1b[0m\x1b[1mcgr ...").
    # (H) NO_COLOR is not enough -- it strips color but keeps bold. Strip all
    # (H) SGR codes, as test_help_command_works already does.
    return _ANSI_RE.sub("", result.stdout)


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
        expected = cs.CLI_MSG_VERSION.format(
            package=cs.PACKAGE_NAME, version=get_version(cs.PACKAGE_NAME)
        )
        assert result.stdout.strip() == expected, (
            f"{flag} output did not match expected format: {repr(result.stdout)}"
        )
        assert result.stderr == "", f"Unexpected stderr for {flag}: {result.stderr}"


def test_help_command_shows_task_grouped_index() -> None:
    result = _RUNNER.invoke(app, ["help"], prog_name="cgr")
    plain = _plain_stdout(result)

    assert result.exit_code == 0
    assert "Usage: cgr [OPTIONS] COMMAND" in plain
    assert ch.PANEL_USE in plain
    assert ch.PANEL_GRAPH in plain
    assert ch.PANEL_MANAGE in plain


def test_help_command_shows_detailed_command_page() -> None:
    result = _RUNNER.invoke(app, ["help", "start"], prog_name="cgr")
    normalized_output = " ".join(_plain_stdout(result).split())

    assert result.exit_code == 0
    assert "Usage: cgr start [OPTIONS]" in normalized_output
    assert "EXAMPLES" in normalized_output
    assert "Delete every project from" in normalized_output
    assert "Requires" in normalized_output
    assert "--update-graph" in normalized_output


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        (["daemon", "logs", "--help"], "Usage: cgr daemon logs [OPTIONS]"),
        (
            ["language", "add-grammar", "--help"],
            "Usage: cgr language add-grammar",
        ),
        (
            ["workspace", "create", "--help"],
            "Usage: cgr workspace create [OPTIONS] NAME",
        ),
        (["help", "daemon", "logs"], "Usage: cgr daemon logs [OPTIONS]"),
    ],
)
def test_nested_help_preserves_full_command_path(args: list[str], usage: str) -> None:
    result = _RUNNER.invoke(app, args, prog_name="cgr")

    assert result.exit_code == 0
    assert usage in _plain_stdout(result)


@pytest.mark.parametrize("group", ["daemon", "language", "workspace"])
def test_group_help_lists_subcommands(group: str) -> None:
    result = _RUNNER.invoke(app, [group, "--help"], prog_name="cgr")
    plain = _plain_stdout(result)

    assert result.exit_code == 0
    assert f"Usage: cgr {group} [OPTIONS] COMMAND" in plain
    assert "Commands:" in plain


def test_help_command_rejects_unknown_command() -> None:
    result = _RUNNER.invoke(app, ["help", "not-a-command"], prog_name="cgr")

    assert result.exit_code == 2
    assert "not a cgr command" in result.stderr


def test_command_summaries_are_single_line() -> None:
    assert all("\n" not in summary for summary in ch.CLI_COMMANDS.values())
