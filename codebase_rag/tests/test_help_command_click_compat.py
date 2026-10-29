"""`cgr help` must work with whichever click typer builds on (#1409).

Newer typer generations vendor click as `typer._click`, so the app's root
command does not descend from the real `click.Group`, and the usage errors
typer raises are not the real `click.ClickException`. The help command
resolves the path on typer's own `TyperGroup` and leaves reporting a mistake
in it to typer, so neither difference reaches it; these tests pin that on
whichever typer is installed.
"""

from __future__ import annotations

import re

import pytest
import typer
from typer.core import TyperCommand, TyperGroup
from typer.testing import CliRunner

from codebase_rag import cli as cgr_cli

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BOX_DRAWING_RE = re.compile(r"[─-╿]")


def _plain(text: str) -> str:
    return " ".join(_BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", text)).split())


def _commands(command: TyperCommand | TyperGroup) -> list[TyperCommand | TyperGroup]:
    found: list[TyperCommand | TyperGroup] = [command]
    if isinstance(command, TyperGroup):
        for child in command.commands.values():
            assert isinstance(child, TyperCommand | TyperGroup), child
            found.extend(_commands(child))
    return found


def test_a_usage_error_in_the_help_path_is_reported_and_exits_2() -> None:
    # It printed click's message and then exited 0.
    result = runner.invoke(cgr_cli.app, ["help", "graph", "nosuch"], prog_name="cgr")

    assert result.exit_code == 2
    assert "No such command 'nosuch'" in _plain(result.stderr)
    assert "Traceback" not in result.output


def test_an_unknown_option_in_the_help_path_is_reported_and_exits_2() -> None:
    result = runner.invoke(
        cgr_cli.app, ["help", "daemon", "logs", "--bogus"], prog_name="cgr"
    )

    assert result.exit_code == 2
    assert "No such option" in _plain(result.stderr)
    assert "Traceback" not in result.output


def test_every_command_is_built_by_the_one_click_typer_uses() -> None:
    # The sub-apps are typer's own groups, not groups of another click run
    # beside it, so nothing in the tree depends on which click that is.
    root = typer.main.get_command(cgr_cli.app)
    assert isinstance(root, TyperGroup)

    commands = _commands(root)

    assert len(commands) > len(root.commands)


@pytest.mark.parametrize(
    "name", ["language", "daemon", "workspace", "trace", "edits", "graph", "port"]
)
def test_root_command_resolves_each_sub_app(name: str) -> None:
    core = typer.main.get_command(cgr_cli.app)
    assert isinstance(core, TyperGroup)

    sub_app = core.get_command(typer.Context(core), name)

    assert isinstance(sub_app, TyperGroup)
    assert sub_app.commands
