"""The generated option tables of the CLI reference (#2426).

`format_cli_options_table` builds a command's rows from its Click parameters,
so a hidden option stays out of the docs as it stays out of `--help`, and an
option the CLI drops leaves the table on the next `make readme`, with the
generated-docs check failing until it is rerun.
"""

from __future__ import annotations

import copy
import re

import click
import pytest
import typer

from codebase_rag import cli as cgr_cli
from codebase_rag import cli_help as ch
from codebase_rag.readme_sections import (
    CLI_OPTION_COMMANDS,
    cli_options_section_name,
    format_cli_options_table,
    generate_all_sections,
)
from codebase_rag.tests.test_cli_reference_options import (
    REFERENCE_PAGE,
    reference_sections,
    table_flags,
)
from scripts.generate_readme import PROJECT_ROOT, replace_sections, stale_sections

START_SECTION = cli_options_section_name(ch.CLICommandName.START)


def _command(name: ch.CLICommandName) -> click.Command:
    return typer.main.get_group(cgr_cli.app).commands[name]


def _demo(*params: click.Parameter) -> click.Command:
    return click.Command("demo", params=list(params))


def _without(command: click.Command, flag: str) -> click.Command:
    trimmed = copy.copy(command)
    trimmed.params = [param for param in command.params if flag not in param.opts]
    return trimmed


def _rows(table: str) -> list[str]:
    return table.splitlines()[2:]


@pytest.fixture(scope="module")
def sections() -> dict[str, str]:
    return generate_all_sections(PROJECT_ROOT)


@pytest.fixture(scope="module")
def page() -> str:
    return REFERENCE_PAGE.read_text(encoding="utf-8")


class TestRows:
    def test_rows_follow_the_command_and_read_as_its_help(self) -> None:
        table = format_cli_options_table(
            _demo(
                click.Option(["-o", "--output"], required=True, help="Write it."),
                click.Option(
                    ["--json/--no-json"], default=True, show_default=True, help="JSON."
                ),
                click.Option(["--size"], default=15, show_default=True, help="Size."),
                click.Option(["--quiet"], is_flag=True, help="Say less."),
            )
        )
        assert _rows(table) == [
            "| `--output`, `-o` | Write it. [required] |",
            "| `--json` / `--no-json` | JSON. [default: json] |",
            "| `--size` | Size. [default: 15] |",
            "| `--quiet` | Say less. |",
        ]

    def test_help_text_is_escaped_for_markdown_outside_code_spans(self) -> None:
        table = format_cli_options_table(
            _demo(
                click.Option(
                    ["--exclude"], help="Skip <repo>/'*/tests/*'; see `a*<b>`."
                )
            )
        )
        assert _rows(table) == [
            "| `--exclude` | Skip &lt;repo>/'\\*/tests/\\*'; see `a*<b>`. |"
        ]

    def test_a_real_command_keeps_its_declaration_order(self) -> None:
        command = _command(ch.CLICommandName.INDEX)
        flags = [
            re.findall(r"`(--[^`]+)`", row)[0]
            for row in _rows(format_cli_options_table(command))
        ]
        assert flags == [
            next(opt for opt in param.opts if opt.startswith("--"))
            for param in command.params
            if isinstance(param, click.Option)
        ]


class TestWhatStaysOut:
    def test_a_hidden_option_is_not_documented(self) -> None:
        table = format_cli_options_table(
            _demo(
                click.Option(["--shown"], help="Shown."),
                click.Option(["--secret"], hidden=True, help="Internal."),
            )
        )
        assert _rows(table) == ["| `--shown` | Shown. |"]

    def test_the_help_option_is_not_a_row(self) -> None:
        for name in CLI_OPTION_COMMANDS:
            assert "--help" not in format_cli_options_table(_command(name))

    def test_an_argument_is_not_an_option_row(self) -> None:
        table = format_cli_options_table(
            _demo(click.Argument(["language"]), click.Option(["--repo-path"]))
        )
        assert _rows(table) == ["| `--repo-path` |  |"]

    def test_a_removed_option_leaves_the_table(self) -> None:
        start = _command(ch.CLICommandName.START)
        full = format_cli_options_table(start)
        trimmed = format_cli_options_table(_without(start, "--capture"))

        assert "`--capture`" in full
        assert "`--capture`" not in trimmed
        assert len(_rows(trimmed)) == len(_rows(full)) - 1


class TestStaleTables:
    """The generated-docs-current check still reports a stale option table."""

    def test_the_committed_tables_are_current(
        self, page: str, sections: dict[str, str]
    ) -> None:
        names = [cli_options_section_name(name) for name in CLI_OPTION_COMMANDS]
        assert not stale_sections(page, sections, names)

    def test_a_hand_deleted_row_is_stale(
        self, page: str, sections: dict[str, str]
    ) -> None:
        row = next(line for line in page.splitlines() if "`--capture`" in line)
        doctored = page.replace(row + "\n", "", 1)

        assert stale_sections(doctored, sections, [START_SECTION]) == [START_SECTION]

    def test_an_option_the_cli_drops_is_stale_then_regenerates_away(
        self, page: str, sections: dict[str, str]
    ) -> None:
        start = _command(ch.CLICommandName.START)
        shrunk = {
            **sections,
            START_SECTION: format_cli_options_table(_without(start, "--capture")),
        }

        assert stale_sections(page, shrunk, [START_SECTION]) == [START_SECTION]
        regenerated = reference_sections(replace_sections(page, shrunk))["start"]
        assert "--capture" not in table_flags(regenerated)
        assert "--exclude" in table_flags(regenerated)


def test_section_names_are_valid_markers() -> None:
    names = [cli_options_section_name(name) for name in CLI_OPTION_COMMANDS]
    assert all(re.fullmatch(r"\w+", name) for name in names)
    assert cli_options_section_name(ch.CLICommandName.DEAD_CODE) == (
        "cli_options_dead_code"
    )
    assert len(set(names)) == len(names)
