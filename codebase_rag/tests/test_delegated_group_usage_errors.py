"""Issue #2416: a usage mistake inside a command group is a usage message and
exit 2, never a traceback.

`daemon`, `graph`, `workspace`, `language`, `trace`, `edits` and `port` were
plain click groups run non-standalone under typer, and a typer that vendors
click only handles its own exception classes, so the real click's
`MissingParameter`/`NoSuchOption`/`NoSuchCommand` escaped as a 50-120 line
traceback with exit 1. They are typer sub-apps of the root app now, built on
the same click as every other command, so these drive them through `cgr`
itself: the outcome a shell sees.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.language import cli as language
from codebase_rag.workspaces import storage

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# typer frames the error in a box; its borders land between wrapped words.
_BOX_DRAWING_RE = re.compile(r"[─-╿]")


def _plain(text: str) -> str:
    return " ".join(_BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", text)).split())


@pytest.mark.parametrize(
    ("args", "usage", "error"),
    [
        (
            ["graph", "callers"],
            "Usage: cgr graph callers",
            "Missing argument 'qualified_name'",
        ),
        (
            ["workspace", "add", "myws", "."],
            "Usage: cgr workspace",
            "No such command 'add'",
        ),
        (
            ["workspace", "show"],
            "Usage: cgr workspace show",
            "Missing argument 'name'",
        ),
        (
            ["daemon", "up", "--bogus"],
            "Usage: cgr daemon up",
            "No such option",
        ),
    ],
)
def test_a_usage_error_is_a_usage_message_and_exit_2(
    args: list[str], usage: str, error: str
) -> None:
    result = runner.invoke(app, args, prog_name="cgr")

    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    err = _plain(result.stderr)
    assert usage in err
    assert error in err
    assert "Traceback" not in result.output


def test_an_aborted_prompt_is_aborted_and_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl+D at a prompt (`cgr language add-grammar` asks for the language
    # name when given neither it nor a URL); nothing is asked of git first.
    monkeypatch.setattr(language, "_source_checkout_root", lambda: tmp_path)
    with patch.object(language, "_run_git") as run_git:
        result = runner.invoke(app, ["language", "add-grammar"], prog_name="cgr")

    assert result.exit_code == 1
    assert "Aborted" in _plain(result.stderr)
    assert "Traceback" not in result.output
    run_git.assert_not_called()


def test_a_real_failure_still_propagates() -> None:
    # Negative: only typer's own usage and abort signals are reported as such.
    with patch(
        "codebase_rag.stack.cli.StackManager", side_effect=RuntimeError("real failure")
    ):
        result = runner.invoke(app, ["daemon", "status"], prog_name="cgr")

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "real failure"


def test_a_valid_command_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Negative.
    monkeypatch.setattr(storage, "list_workspaces", lambda: ["done"])

    result = runner.invoke(app, ["workspace", "list"], prog_name="cgr")

    assert result.exit_code == 0, result.output
    assert result.stdout == "done\n"


@pytest.mark.parametrize(
    "group", ["graph", "workspace", "daemon", "language", "trace", "edits", "port"]
)
def test_group_help_still_exits_0(group: str) -> None:
    # Negative: `--help` ends in typer's own Exit(0), not an error.
    result = runner.invoke(app, [group, "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_through_the_cli_the_usage_error_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["graph", "callers"])

    assert result.exit_code == 2
    assert "Missing argument" in _plain(result.output)
