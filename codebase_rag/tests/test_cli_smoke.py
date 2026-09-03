import re
import subprocess
import sys
from importlib.metadata import version as get_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag.cli import app

# Deadline for a CLI subprocess, not a performance assertion. Starting the CLI
# costs ~12s because `--help` imports the whole parser stack before it can
# render, so a 30s deadline left barely 2.5x headroom and the tests below
# flaked under `pytest -n 2` while passing alone (issue #1655). These tests ask
# "does the CLI start and print", and a generous deadline does not weaken that;
# the startup cost itself is the separate question.
_CLI_TIMEOUT_SECONDS = 120

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# rich draws the options table with box-drawing borders whose glyphs land
# BETWEEN the words of a wrapped cell (legacy Windows consoles wrap one column
# narrower than others), so phrase asserts must strip them along with the ANSI
# codes before whitespace-joining.
_BOX_DRAWING_RE = re.compile(r"[─-╿]")
_RUNNER = CliRunner()


def _normalized_help(stdout: str) -> str:
    plain = _BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", stdout))
    return " ".join(plain.split())


def test_help_command_works() -> None:
    repo_root = Path(__file__).parent.parent.parent

    result = subprocess.run(
        [sys.executable, "-m", "codebase_rag.cli", "--help"],
        check=False,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        timeout=_CLI_TIMEOUT_SECONDS,
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
            encoding=cs.ENCODING_UTF8,
            timeout=_CLI_TIMEOUT_SECONDS,
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

    # rich colourises help when it detects an ANSI-capable log sink (GitHub
    # Actions among them), so raw stdout carries escape codes there and
    # plain-substring asserts must run on the normalised text.
    plain_stdout = _normalized_help(result.stdout)
    assert result.exit_code == 0
    assert "Usage: cgr [OPTIONS] COMMAND" in plain_stdout
    assert ch.PANEL_USE in plain_stdout
    assert ch.PANEL_GRAPH in plain_stdout
    assert ch.PANEL_MANAGE in plain_stdout


def test_help_command_shows_detailed_command_page() -> None:
    result = _RUNNER.invoke(app, ["help", "start"], prog_name="cgr")
    normalized_output = _normalized_help(result.stdout)

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
    assert usage in result.stdout


@pytest.mark.parametrize("group", ["daemon", "language", "workspace"])
def test_group_help_lists_subcommands(group: str) -> None:
    result = _RUNNER.invoke(app, [group, "--help"], prog_name="cgr")

    assert result.exit_code == 0
    assert f"Usage: cgr {group} [OPTIONS] COMMAND" in result.stdout
    assert "Commands:" in result.stdout


def test_help_command_rejects_unknown_command() -> None:
    result = _RUNNER.invoke(app, ["help", "not-a-command"], prog_name="cgr")

    assert result.exit_code == 2
    assert "not a cgr command" in result.stderr


def test_every_cli_subprocess_uses_the_shared_deadline() -> None:
    # Guards the class rather than the two sites fixed in #1655: a new CLI
    # subprocess written with its own literal deadline would reintroduce the
    # flake silently, and only under parallel load, which is the hardest kind
    # of failure to attribute. Matching on the value rather than on a forbidden
    # number so any literal fails, not just the one that bit us.
    source = Path(__file__).read_text(encoding=cs.ENCODING_UTF8)
    values = set(re.findall(r"timeout=(\w+)", source))
    assert values == {"_CLI_TIMEOUT_SECONDS"}, (
        f"CLI subprocesses must share the deadline constant; found {sorted(values)}"
    )


def test_command_summaries_are_single_line() -> None:
    assert all("\n" not in summary for summary in ch.CLI_COMMANDS.values())


def test_every_command_name_has_a_registry_entry() -> None:
    # CLI_COMMANDS feeds the generated command tables (README, help); a
    # CLICommandName member missing from it silently drops the command there.
    assert set(ch.CLI_COMMANDS) == set(ch.CLICommandName)
