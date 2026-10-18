"""The GitHub Sponsors pledge: a line under `--version`, and the line a
finished command ends on in a terminal."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from codebase_rag import cli
from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag.config import settings

_PLEDGE = cs.CLI_MSG_SPONSOR_PLEDGE.format(url=cs.SPONSOR_URL)


@pytest.fixture
def pledging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "QUIET", False)
    monkeypatch.setattr(settings, "SPONSOR_PLEDGE", True)


@pytest.fixture
def on_a_terminal(monkeypatch: pytest.MonkeyPatch, pledging: None) -> None:
    # CliRunner's stderr is never a terminal, so the group is told it is one.
    real = cli.sponsor_pledge
    monkeypatch.setattr(
        cli, "sponsor_pledge", lambda command, terminal: real(command, True)
    )


@pytest.mark.usefixtures("pledging")
def test_a_finished_command_on_a_terminal_gets_the_pledge() -> None:
    assert cli.sponsor_pledge(ch.CLICommandName.STATS, terminal=True) == _PLEDGE


@pytest.mark.usefixtures("pledging")
def test_off_a_terminal_there_is_no_pledge() -> None:
    assert cli.sponsor_pledge(ch.CLICommandName.STATS, terminal=False) is None


@pytest.mark.usefixtures("pledging")
@pytest.mark.parametrize(
    "command", [ch.CLICommandName.HELP, ch.CLICommandName.MCP_SERVER]
)
def test_help_and_the_mcp_server_get_no_pledge(command: str) -> None:
    assert cli.sponsor_pledge(command, terminal=True) is None


@pytest.mark.usefixtures("pledging")
def test_quiet_drops_the_pledge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "QUIET", True)
    assert cli.sponsor_pledge(ch.CLICommandName.STATS, terminal=True) is None


@pytest.mark.usefixtures("pledging")
def test_the_setting_turns_the_pledge_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SPONSOR_PLEDGE", False)
    assert cli.sponsor_pledge(ch.CLICommandName.STATS, terminal=True) is None


def test_the_setting_reads_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CGR_SPONSOR_PLEDGE", "false")
    assert type(settings)().SPONSOR_PLEDGE is False


def test_the_pledge_names_its_opt_out() -> None:
    assert "CGR_SPONSOR_PLEDGE=false" in _PLEDGE


@pytest.mark.usefixtures("on_a_terminal")
def test_the_pledge_follows_a_finished_command_on_stderr() -> None:
    result = CliRunner().invoke(cli.app, ["language", "list-languages"])

    assert result.exit_code == 0, result.output
    assert cs.SPONSOR_URL in result.stderr
    assert cs.SPONSOR_URL not in result.stdout


@pytest.mark.usefixtures("on_a_terminal")
def test_a_failed_command_gets_no_pledge() -> None:
    result = CliRunner().invoke(cli.app, ["graph-loader", "/no/such/graph.json"])

    assert result.exit_code != 0
    assert cs.SPONSOR_URL not in result.output


@pytest.mark.usefixtures("on_a_terminal")
def test_cgr_help_gets_no_pledge() -> None:
    result = CliRunner().invoke(cli.app, ["help"])

    assert result.exit_code == 0, result.output
    assert cs.SPONSOR_URL not in result.output


def test_the_version_flag_names_the_sponsors_page() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])

    assert result.exit_code == 0, result.output
    assert cs.CLI_MSG_SPONSOR.format(url=cs.SPONSOR_URL) in result.stdout
