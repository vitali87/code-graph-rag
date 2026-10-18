"""The generated option tables of the CLI reference (#2426).

`format_cli_options_table` builds a command's rows from its Click parameters,
so a hidden option stays out of the docs as it stays out of `--help`, and an
option the CLI drops leaves the table on the next `make readme`, with the
generated-docs check failing until it is rerun.

The demo commands are built by typer, as the cgr commands are, so their
options are whichever Click classes the installed typer builds with: the
locked typer vendors Click as `typer._click`, and a generator that filtered on
the real `click.Option` wrote every table empty.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable

import click
import pytest
import typer
from typer.core import TyperCommand

from codebase_rag import cli_help as ch
from codebase_rag import readme_sections
from codebase_rag.readme_sections import (
    CLI_OPTION_COMMANDS,
    cli_option_command,
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
OPTION_PARAM_TYPE = "option"


def _demo(function: Callable[..., None]) -> TyperCommand:
    demo = typer.Typer(add_completion=False)
    demo.command()(function)
    command = typer.main.get_command(demo)
    assert isinstance(command, TyperCommand)
    return command


def _without(command: TyperCommand, flag: str) -> TyperCommand:
    trimmed = copy.copy(command)
    trimmed.params = [param for param in command.params if flag not in param.opts]
    return trimmed


def _visible_options(command: TyperCommand) -> list[str]:
    # Duck-typed on `param_type_name`, independently of the generator's own
    # filter, so the two cannot agree on finding nothing.
    return [
        next(opt for opt in param.opts if opt.startswith("--"))
        for param in command.params
        if param.param_type_name == OPTION_PARAM_TYPE
        and not getattr(param, "hidden", False)
    ]


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
        def demo(
            output: str = typer.Option(..., "-o", "--output", help="Write it."),
            json: bool = typer.Option(True, "--json/--no-json", help="JSON."),
            size: int = typer.Option(15, "--size", help="Size."),
            quiet: bool = typer.Option(False, "--quiet", help="Say less."),
        ) -> None: ...

        assert _rows(format_cli_options_table(_demo(demo))) == [
            "| `--output`, `-o` | Write it. [required] |",
            "| `--json` / `--no-json` | JSON. [default: json] |",
            "| `--size` | Size. [default: 15] |",
            "| `--quiet` | Say less. |",
        ]

    def test_help_text_is_escaped_for_markdown_outside_code_spans(self) -> None:
        def demo(
            exclude: str | None = typer.Option(
                None, "--exclude", help="Skip <repo>/'*/tests/*'; see `a*<b>`."
            ),
        ) -> None: ...

        assert _rows(format_cli_options_table(_demo(demo))) == [
            "| `--exclude` | Skip &lt;repo>/'\\*/tests/\\*'; see `a*<b>`. |"
        ]

    def test_a_real_command_keeps_its_declaration_order(self) -> None:
        command = cli_option_command(ch.CLICommandName.INDEX)
        flags = [
            re.findall(r"`(--[^`]+)`", row)[0]
            for row in _rows(format_cli_options_table(command))
        ]
        assert flags
        assert flags == _visible_options(command)


class TestWhicheverClickTyperUses:
    """The locked typer vendors Click as `typer._click`, so the options it
    builds are not the real `click.Option`; filtering on that class found none
    and every generated table came out empty on every platform."""

    @pytest.mark.parametrize("name", CLI_OPTION_COMMANDS)
    def test_every_visible_option_of_a_documented_command_is_a_row(
        self, name: ch.CLICommandName
    ) -> None:
        command = cli_option_command(name)
        options = _visible_options(command)
        assert options, f"cgr {name} has no options; the check would pass vacuously"
        assert len(_rows(format_cli_options_table(command))) == len(options)

    def test_rows_do_not_hinge_on_the_real_click_option_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The vendoring typer's condition, emulated for whichever typer is
        # installed: no option typer builds descends from `click.Option`.
        start = cli_option_command(ch.CLICommandName.START)

        class UnrelatedOption:
            pass

        monkeypatch.setattr(click, "Option", UnrelatedOption)
        table = format_cli_options_table(start)

        assert "`--capture`" in table
        assert len(_rows(table)) == len(_visible_options(start))

    def test_a_group_in_place_of_a_documented_command_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A group has no option table of its own; refusing it keeps a class
        # mismatch from turning back into a silently empty table.
        group = typer.Typer()

        @group.command()
        def run() -> None: ...

        app = typer.Typer()
        app.add_typer(group, name=ch.CLICommandName.START)
        monkeypatch.setattr(readme_sections, "cli_app", app)

        with pytest.raises(TypeError, match="cgr start is not a TyperCommand"):
            cli_option_command(ch.CLICommandName.START)


class TestWhatStaysOut:
    def test_a_hidden_option_is_not_documented(self) -> None:
        def demo(
            shown: str | None = typer.Option(None, "--shown", help="Shown."),
            secret: str | None = typer.Option(
                None, "--secret", hidden=True, help="Internal."
            ),
        ) -> None: ...

        assert _rows(format_cli_options_table(_demo(demo))) == [
            "| `--shown` | Shown. |"
        ]

    def test_the_help_option_is_not_a_row(self) -> None:
        for name in CLI_OPTION_COMMANDS:
            table = format_cli_options_table(cli_option_command(name))
            assert _rows(table)
            assert "--help" not in table

    def test_an_argument_is_not_an_option_row(self) -> None:
        def demo(
            language: str = typer.Argument(...),
            repo_path: str | None = typer.Option(None, "--repo-path"),
        ) -> None: ...

        assert _rows(format_cli_options_table(_demo(demo))) == ["| `--repo-path` |  |"]

    def test_a_removed_option_leaves_the_table(self) -> None:
        start = cli_option_command(ch.CLICommandName.START)
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
        start = cli_option_command(ch.CLICommandName.START)
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
