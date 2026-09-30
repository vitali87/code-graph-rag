"""Issue #2416: a usage mistake inside a delegated command group is a usage
message and exit 2, never a traceback.

`daemon`, `graph`, `workspace`, `language`, `trace` and `edits` are plain
click groups run by `_run_delegated_group` with `standalone_mode=False`, so
their usage errors propagate to typer. A typer that vendors click (the one
`uv tool install` resolves) only handles its own exception classes, so the
real click's `MissingParameter`/`NoSuchOption`/`NoSuchCommand` escaped as a
50-120 line traceback with exit 1. The boundary handles them itself now, so
the outcome does not depend on which typer is installed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest
import typer
from typer.testing import CliRunner

from codebase_rag.cli import _run_delegated_group, app
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.stack.cli import cli as daemon_cli
from codebase_rag.workspaces.cli import cli as workspace_cli

runner = CliRunner()


def _ctx(path: str, args: list[str]) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    ctx.command_path = path
    return ctx


@pytest.mark.parametrize(
    ("group", "path", "args", "usage", "error"),
    [
        (
            graph_cli,
            "cgr graph",
            ["callers"],
            "Usage: cgr graph callers",
            "Missing argument 'QUALIFIED_NAME'",
        ),
        (
            workspace_cli,
            "cgr workspace",
            ["add", "myws", "."],
            "Usage: cgr workspace",
            "No such command 'add'",
        ),
        (
            workspace_cli,
            "cgr workspace",
            ["show"],
            "Usage: cgr workspace show",
            "Missing argument 'NAME'",
        ),
        (
            daemon_cli,
            "cgr daemon",
            ["up", "--bogus"],
            "Usage: cgr daemon up",
            "No such option",
        ),
    ],
)
def test_a_usage_error_is_a_usage_message_and_exit_2(
    capsys: pytest.CaptureFixture[str],
    group: click.Group,
    path: str,
    args: list[str],
    usage: str,
    error: str,
) -> None:
    ctx = _ctx(path, args)
    with pytest.raises(typer.Exit) as raised:
        _run_delegated_group(group, ctx)

    assert raised.value.exit_code == 2
    err = capsys.readouterr().err
    assert usage in err
    assert error in err
    assert "Traceback" not in err


@click.group()
def _prompting() -> None:
    pass


@_prompting.command()
def ask() -> None:
    raise click.Abort


@_prompting.command()
def boom() -> None:
    raise RuntimeError("real failure")


@_prompting.command()
def fine() -> None:
    click.echo("done")


def test_an_aborted_prompt_is_aborted_and_exit_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Ctrl+D at `click.confirm` (e.g. `cgr language add-grammar`).
    ctx = _ctx("cgr prompting", ["ask"])
    with pytest.raises(typer.Exit) as raised:
        _run_delegated_group(_prompting, ctx)

    assert raised.value.exit_code == 1
    assert "Aborted!" in capsys.readouterr().err


def test_a_real_failure_still_propagates() -> None:
    # Negative: only click's own usage and abort signals are handled here.
    ctx = _ctx("cgr prompting", ["boom"])
    with pytest.raises(RuntimeError, match="real failure"):
        _run_delegated_group(_prompting, ctx)


def test_a_valid_command_still_runs(capsys: pytest.CaptureFixture[str]) -> None:
    # Negative.
    _run_delegated_group(_prompting, _ctx("cgr prompting", ["fine"]))

    assert capsys.readouterr().out == "done\n"


@pytest.mark.parametrize("group", ["graph", "workspace", "daemon", "language"])
def test_group_help_still_exits_0(group: str) -> None:
    # Negative: `--help` ends in click's own Exit(0), not an error.
    result = runner.invoke(app, [group, "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_through_the_cli_the_usage_error_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["graph", "callers"])

    assert result.exit_code == 2
    assert "Missing argument" in click.unstyle(result.output)
