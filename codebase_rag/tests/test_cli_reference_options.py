"""The CLI reference documents every option of the commands it describes (#2426).

The per-command option tables in `docs/guide/cli-reference.md` were written by
hand while the command overview above them came from the help registry, so
they drifted: `cgr start` listed 8 of its 21 options, and `cgr index`,
`cgr export` and `cgr mcp-server` had no table at all. These tests read the
page and compare it with the commands' own Click parameters.
"""

from __future__ import annotations

import re

import click
import pytest
import typer
from typer.core import TyperGroup

from codebase_rag import cli as cgr_cli
from codebase_rag import cli_help as ch
from codebase_rag import readme_sections
from scripts.generate_readme import PROJECT_ROOT

REFERENCE_PAGE = PROJECT_ROOT / "docs" / "guide" / "cli-reference.md"
GRAPH_SCHEMA_PAGE = PROJECT_ROOT / "docs" / "architecture" / "graph-schema.md"

# The commands the reference page has an option table for, pinned here rather
# than read from the generator so the page is checked against the CLI itself.
DOCUMENTED_COMMANDS = (
    ch.CLICommandName.START,
    ch.CLICommandName.INDEX,
    ch.CLICommandName.EXPORT,
    ch.CLICommandName.OPTIMIZE,
    ch.CLICommandName.MCP_SERVER,
    ch.CLICommandName.STATS,
    ch.CLICommandName.DEAD_CODE,
    ch.CLICommandName.DUPLICATES,
)

COMMAND_HEADING = re.compile(r"^### `cgr ([a-z][a-z-]*)`$", re.MULTILINE)
ANY_HEADING = re.compile(r"^#{1,3} ", re.MULTILINE)
TABLE_FLAG = re.compile(r"`(-[^`\s]+)`")

DOC_FILES = (PROJECT_ROOT / "README.md", *sorted((PROJECT_ROOT / "docs").rglob("*.md")))
INLINE_CGR_COMMAND = re.compile(r"`cgr ([a-z][a-z-]*)")
FENCED_CGR_COMMAND = re.compile(r"^\s*(?:\$ )?cgr ([a-z][a-z-]*)", re.MULTILINE)


def _commands() -> dict[str, click.Command]:
    return typer.main.get_group(cgr_cli.app).commands


def visible_flags(command: click.Command) -> list[str]:
    # Duck-typed on `param_type_name`: a typer that vendors click builds
    # options that are not `click.Option`, and an isinstance filter would then
    # find no options and pass every check here vacuously (#1409).
    flags: list[str] = []
    for param in command.params:
        if param.param_type_name != "option" or getattr(param, "hidden", False):
            continue
        flags.extend([*param.opts, *param.secondary_opts])
    return flags


def reference_sections(page: str) -> dict[str, str]:
    """Each `### cgr NAME` section's text, up to the next heading."""
    sections: dict[str, str] = {}
    for heading in COMMAND_HEADING.finditer(page):
        following = ANY_HEADING.search(page, heading.end())
        end = following.start() if following else len(page)
        sections[heading.group(1)] = page[heading.end() : end]
    return sections


def table_flags(section: str) -> set[str]:
    """Flags named in the first cell of the section's table rows. A flag that
    only appears in an example or in prose does not document the option."""
    flags: set[str] = set()
    for line in section.splitlines():
        if line.startswith("| "):
            flags.update(TABLE_FLAG.findall(line.split(" | ", 1)[0]))
    return flags


@pytest.fixture(scope="module")
def sections() -> dict[str, str]:
    return reference_sections(REFERENCE_PAGE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", DOCUMENTED_COMMANDS)
def test_every_option_is_in_its_reference_table(
    name: ch.CLICommandName, sections: dict[str, str]
) -> None:
    flags = visible_flags(_commands()[name])
    assert flags, f"cgr {name} has no options; the check would pass vacuously"
    assert name in sections, f"no `### cgr {name}` section"
    documented = table_flags(sections[name])
    missing = [flag for flag in flags if flag not in documented]
    assert not missing, f"cgr {name} options missing from its table: {missing}"


@pytest.mark.parametrize("name", DOCUMENTED_COMMANDS)
def test_no_reference_table_lists_an_option_the_command_lacks(
    name: ch.CLICommandName, sections: dict[str, str]
) -> None:
    extra = table_flags(sections[name]) - set(visible_flags(_commands()[name]))
    assert not extra, f"cgr {name}'s table lists options it does not have: {extra}"


def test_every_command_heading_with_options_has_a_generated_table(
    sections: dict[str, str],
) -> None:
    """A new `### cgr NAME` section cannot bring back a hand-written table:
    a command with options needs its generated block."""
    assert set(readme_sections.CLI_OPTION_COMMANDS) == set(DOCUMENTED_COMMANDS)
    commands = _commands()
    for name, section in sections.items():
        assert name in commands, f"`### cgr {name}` names no cgr command"
        command = commands[name]
        # TyperGroup, not `click.Group`: a vendoring typer's groups are not
        # the real Click's, so that check could never skip one (#1409).
        if isinstance(command, TyperGroup) or not visible_flags(command):
            continue
        section_name = readme_sections.cli_options_section_name(ch.CLICommandName(name))
        marker = f"<!-- SECTION:{section_name} -->"
        assert marker in section, f"`### cgr {name}` has no {marker} block"


def test_the_docs_name_only_real_cgr_commands() -> None:
    """graph-schema.md pointed readers at a `cgr diff` command that does not
    exist (`No such command 'diff'`)."""
    commands = set(_commands())
    unknown: list[str] = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for pattern in (INLINE_CGR_COMMAND, FENCED_CGR_COMMAND):
            unknown.extend(
                f"{path.relative_to(PROJECT_ROOT)}: cgr {name}"
                for name in pattern.findall(text)
                if name not in commands
            )
    assert not unknown, unknown


def test_the_edge_site_note_names_diff_index() -> None:
    # The structural diff that ignores edge-site properties is diff-index's
    # (services/graph_diff.py); the sentence must not just be dropped.
    schema = GRAPH_SCHEMA_PAGE.read_text(encoding="utf-8")
    assert "`cgr diff-index` treats these properties as location" in schema
