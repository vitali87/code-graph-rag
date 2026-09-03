import ast
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
# Call names that start a real process, so a deadline applies to them.
_SUBPROCESS_LAUNCHERS = frozenset({"run", "Popen", "check_output", "check_call"})

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


def _deadlines_not_using_the_constant(tree: ast.AST) -> list[int]:
    """Lines of calls whose `timeout=` is anything but the shared constant.

    AST rather than a regex on the source: `timeout = 30` with spaces and
    `timeout=_CLI_TIMEOUT_SECONDS // 8` both read as compliant to a textual
    scan, and both are legal at these call sites. Mirrors the detector in
    `test_frontend_subprocess_timeouts.py`, which exists for the same class.

    A `**{"timeout": 30}` splat stays invisible to this too -- the keyword has
    no `arg` -- so the sibling gate's rule applies: over-reporting is the safe
    direction, and this one cannot see that shape at all.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        timeout = next((kw for kw in node.keywords if kw.arg == "timeout"), None)
        if timeout is None:
            continue
        if not (
            isinstance(timeout.value, ast.Name)
            and timeout.value.id == "_CLI_TIMEOUT_SECONDS"
        ):
            found.append(node.lineno)
    return found


def test_cli_spawns_in_this_file_share_the_deadline() -> None:
    # A new CLI subprocess written with its own literal deadline reintroduces
    # the #1655 flake silently, and only under parallel load -- the hardest
    # kind of failure to attribute.
    tree = ast.parse(Path(__file__).read_text(encoding=cs.ENCODING_UTF8))
    offenders = _deadlines_not_using_the_constant(tree)
    assert not offenders, (
        "CLI subprocesses here must use _CLI_TIMEOUT_SECONDS; "
        f"lines with another deadline: {offenders}"
    )


def _cli_spawn_lines(tree: ast.AST) -> list[int]:
    """Lines that launch the CLI as a SUBPROCESS.

    Matched on the argv, not on the text of the file: twenty test files import
    `codebase_rag.cli` to drive it in-process through `CliRunner`, and those
    pay no startup cost and need no deadline. A substring scan counts them as
    spawns, which is how the first version of this gate reported twenty
    offenders that were all imports.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else None
        if name is None and isinstance(target, ast.Name):
            name = target.id
        if name not in _SUBPROCESS_LAUNCHERS or not node.args:
            continue
        for element in ast.walk(node.args[0]):
            if (
                isinstance(element, ast.Constant)
                and isinstance(element.value, str)
                and cs.CLI_MODULE_INVOCATION in element.value
            ):
                found.append(node.lineno)
                break
    return found


def test_no_other_test_file_spawns_the_cli() -> None:
    """What makes the file-scoped gate above sufficient coverage.

    That gate can only police this file, so its reach is a claim about the
    suite rather than about itself. A CLI spawn elsewhere inherits none of the
    deadline discipline and nothing reports the hole, so assert the premise
    instead of assuming it.
    """
    here = Path(__file__)
    others = sorted(
        path.name
        for path in here.parent.rglob("test_*.py")
        if path != here
        and _cli_spawn_lines(ast.parse(path.read_text(encoding=cs.ENCODING_UTF8)))
    )
    assert not others, (
        "these files spawn the CLI and are outside this deadline gate: "
        f"{others}; bring them under a shared constant or widen the gate"
    )


def test_the_spawn_detector_separates_spawns_from_imports() -> None:
    """A control: the premise test above passes vacuously if nothing matches.

    An empty offender list and a detector that never fires are the same
    assertion, which is the shape this repo keeps meeting.
    """
    assert _cli_spawn_lines(
        ast.parse("subprocess.run([sys.executable, '-m', 'codebase_rag.cli'])")
    ) == [1]
    assert _cli_spawn_lines(
        ast.parse("subprocess.Popen([sys.executable, '-m', 'codebase_rag.cli'])")
    ) == [1]
    # An in-process CliRunner test imports the same module and must NOT count.
    assert _cli_spawn_lines(ast.parse("from codebase_rag.cli import app")) == []
    assert _cli_spawn_lines(ast.parse("_RUNNER.invoke(app, ['help'])")) == []
    # This file is the one place that really does spawn it.
    assert len(_cli_spawn_lines(ast.parse(Path(__file__).read_text("utf-8")))) == 2


def test_the_detector_recognises_compliant_and_offending_deadlines() -> None:
    """A control: the gate above passes vacuously if the matcher never fires."""
    assert _deadlines_not_using_the_constant(
        ast.parse("subprocess.run(['x'], timeout=30)")
    ) == [1]
    assert _deadlines_not_using_the_constant(
        ast.parse("subprocess.run(['x'], timeout = 30)")
    ) == [1]
    assert _deadlines_not_using_the_constant(
        ast.parse("subprocess.run(['x'], timeout=_CLI_TIMEOUT_SECONDS // 8)")
    ) == [1]
    assert _deadlines_not_using_the_constant(
        ast.parse("subprocess.run(['x'], timeout=_OTHER)")
    ) == [1]
    assert (
        _deadlines_not_using_the_constant(
            ast.parse("subprocess.run(['x'], timeout=_CLI_TIMEOUT_SECONDS)")
        )
        == []
    )
    # No `timeout=` at all is a different gate's business
    # (test_frontend_subprocess_timeouts), so this one stays quiet.
    assert _deadlines_not_using_the_constant(ast.parse("subprocess.run(['x'])")) == []


def test_command_summaries_are_single_line() -> None:
    assert all("\n" not in summary for summary in ch.CLI_COMMANDS.values())


def test_every_command_name_has_a_registry_entry() -> None:
    # CLI_COMMANDS feeds the generated command tables (README, help); a
    # CLICommandName member missing from it silently drops the command there.
    assert set(ch.CLI_COMMANDS) == set(ch.CLICommandName)
