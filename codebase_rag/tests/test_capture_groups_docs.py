"""Capture groups are documented from the capture model itself (#2584).

`--capture` named 5 of the 10 groups, `CGR_CAPTURE` was in no configuration
page, and the schema listed `Parameter`, `Field` and `EnumVariant` as ordinary
schema although a default index writes none of them. The help and the docs
are now generated from `CaptureGroup`, `CAPTURE_GROUP_RELS`,
`CAPTURE_GROUP_NODE_LABELS` and `DEFAULT_CAPTURE_GROUPS`, and these tests
read the rendered help and the committed pages independently of the
generator, so a group added to the model without reaching both fails here.

The negative tests pin what must not move with it: the default selection,
how the resolver reads its tokens, and every other command's help.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
import typer
from loguru import logger
from typer.core import TyperCommand, TyperGroup, TyperOption
from typer.testing import CliRunner

from codebase_rag import cli as cgr_cli
from codebase_rag import constants as cs
from codebase_rag.capture import capture_help, resolve_capture, split_spec
from codebase_rag.config import settings
from codebase_rag.readme_sections import format_capture_groups_table
from codebase_rag.types_defs import NODE_SCHEMAS, RELATIONSHIP_SCHEMAS
from scripts.generate_readme import PROJECT_ROOT

SCHEMA_PAGE = PROJECT_ROOT / "docs" / "architecture" / "graph-schema.md"
CONFIG_PAGE = PROJECT_ROOT / "docs" / "getting-started" / "configuration.md"
DATA_FLOW_PAGE = PROJECT_ROOT / "docs" / "architecture" / "data-flow-edges.md"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
README = PROJECT_ROOT / "README.md"
CAPTURE_GROUPS_LINK = "graph-schema.md#capture-groups"
CAPTURE_COMMANDS = ("start", "index")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Box-drawing glyphs land between the words of a wrapped rich help cell.
_BOX_DRAWING_RE = re.compile(r"[─-╿]")
_RUNNER = CliRunner()


def _help(*command: str) -> str:
    result = _RUNNER.invoke(cgr_cli.app, [*command, "--help"])
    assert result.exit_code == 0, result.output
    plain = _BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", result.output))
    return " ".join(plain.split())


def _section(page: str, name: str) -> str:
    match = re.search(
        rf"<!-- SECTION:{name} -->\n(.*?)<!-- /SECTION:{name} -->", page, re.DOTALL
    )
    assert match, f"no {name} section"
    return match.group(1)


def _rows(table: str) -> list[list[str]]:
    lines = [line for line in table.splitlines() if line.startswith("| ")]
    return [
        line.removeprefix("| ").removesuffix(" |").split(" | ") for line in lines[1:]
    ]


_OPT_IN_NOTE = re.compile(r"^(\w+) \(opt-in: \[`(\w+)`\]\(#capture-groups\)\)$")


def _opt_in_note(cell: str) -> tuple[str, str | None]:
    """A schema-table cell's name, and the group its opt-in note points at."""
    match = _OPT_IN_NOTE.match(cell)
    return (match.group(1), match.group(2)) if match else (cell, None)


def _names(cell: str) -> list[str]:
    return [] if cell == "-" else cell.split(", ")


def _resolver_defaults() -> list[cs.CaptureGroup]:
    # Read from the resolver, not from DEFAULT_CAPTURE_GROUPS, so the docs
    # are checked against what an index with no configuration really does.
    enabled = resolve_capture([]).enabled_rels
    return [g for g in cs.CaptureGroup if cs.CAPTURE_GROUP_RELS[g] <= enabled]


def _group_rows(page: str) -> dict[str, list[str]]:
    rows = _rows(_section(page, "capture_groups"))
    return {row[0].strip("`"): row for row in rows}


@pytest.fixture(scope="module")
def schema_page() -> str:
    return SCHEMA_PAGE.read_text(encoding="utf-8")


@pytest.fixture
def no_env_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CGR_CAPTURE", "")


def _warnings(tokens: list[str]) -> list[str]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        resolve_capture(tokens)
    finally:
        logger.remove(sink)
    return [message.rstrip() for message in messages]


def _commands() -> Iterator[tuple[str, TyperCommand]]:
    # Duck-typed: typer vendors click, so its commands and options are not
    # `click.Command` / `click.Option` and an isinstance filter finds nothing.
    def walk(prefix: str, group: TyperGroup) -> Iterator[tuple[str, TyperCommand]]:
        for name, command in group.commands.items():
            path = f"{prefix} {name}".strip()
            yield path, command
            if isinstance(command, TyperGroup):
                yield from walk(path, command)

    yield from walk("", typer.main.get_group(cgr_cli.app))


def _options(command: TyperCommand) -> list[TyperOption]:
    return [p for p in command.params if isinstance(p, TyperOption)]


def _option(command: str, flag: str) -> TyperOption:
    return next(p for p in _options(dict(_commands())[command]) if flag in p.opts)


class TestTheHelp:
    @pytest.mark.parametrize("command", CAPTURE_COMMANDS)
    def test_it_names_every_group(self, command: str) -> None:
        text = _help(command)
        missing = [g.value for g in cs.CaptureGroup if g.value not in text]
        assert not missing, f"cgr {command} --help omits {missing}"

    @pytest.mark.parametrize("command", CAPTURE_COMMANDS)
    def test_it_separates_the_defaults_from_the_opt_in_groups(
        self, command: str
    ) -> None:
        defaults = _resolver_defaults()
        opt_in = [g for g in cs.CaptureGroup if g not in defaults]
        text = _help(command)
        assert f"defaults ({', '.join(defaults)})" in text, text
        assert f"Opt-in groups: {', '.join(opt_in)}." in text, text

    @pytest.mark.parametrize("command", CAPTURE_COMMANDS)
    def test_it_says_what_plus_and_minus_accept(self, command: str) -> None:
        text = _help(command)
        assert "+NAME adds and -NAME drops a GROUP or a relationship type" in text
        example = re.search(r"relationship type such as (\w+)", text)
        assert example, text
        # The example is a real token: a relationship type, and not also a
        # group name, which the resolver would read as the group.
        assert example.group(1) in cs.RelationshipType.__members__.values()
        assert example.group(1).lower() not in cs.CaptureGroup.__members__.values()


class TestTheCaptureGroupsTable:
    def test_the_schema_page_has_the_section(self, schema_page: str) -> None:
        assert re.search(r"^## Capture Groups$", schema_page, re.MULTILINE)

    def test_one_row_per_group_in_declaration_order(self, schema_page: str) -> None:
        assert list(_group_rows(schema_page)) == [g.value for g in cs.CaptureGroup]

    def test_the_default_column_is_what_the_resolver_enables(
        self, schema_page: str
    ) -> None:
        defaults = {g.value for g in _resolver_defaults()}
        marked = {
            name for name, row in _group_rows(schema_page).items() if row[1] != "-"
        }
        assert marked == defaults

    def test_each_row_lists_the_groups_labels_and_relationships(
        self, schema_page: str
    ) -> None:
        rows = _group_rows(schema_page)
        for group in cs.CaptureGroup:
            row = rows[group.value]
            labels = cs.CAPTURE_GROUP_NODE_LABELS.get(group, frozenset())
            assert set(_names(row[2])) == {str(label) for label in labels}, group
            assert set(_names(row[3])) == {
                str(rel) for rel in cs.CAPTURE_GROUP_RELS[group]
            }, group
            assert row[4], f"{group} has no description"

    def test_a_group_missing_from_the_table_is_caught(self, schema_page: str) -> None:
        # Not vacuous: the check above sees a row that is not there.
        row = next(
            line for line in schema_page.splitlines() if line.startswith("| `io` |")
        )
        doctored = schema_page.replace(row + "\n", "", 1)
        assert cs.CaptureGroup.IO.value not in _group_rows(doctored)

    def test_the_syntax_is_explained(self, schema_page: str) -> None:
        section = schema_page[schema_page.index("## Capture Groups") :]
        section = section[: section.index("\n## ", 1)]
        for token in (
            "CGR_CAPTURE",
            "--capture",
            f"`{cs.CAPTURE_TOKEN_ALL}`",
            f"`{cs.CAPTURE_TOKEN_NONE}`",
            f"`{cs.CAPTURE_ADD_PREFIX}TYPE`",
            f"`{cs.CAPTURE_DROP_PREFIX}TYPE`",
        ):
            assert token in section, token


class TestGeneratedFromTheModel:
    """The help and the table follow the capture model rather than a copy of
    it: a group moved in or out of the defaults moves in both."""

    @pytest.fixture
    def only_structure_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cs, "DEFAULT_CAPTURE_GROUPS", frozenset({cs.CaptureGroup.STRUCTURE})
        )

    def test_the_help(self, only_structure_by_default: None) -> None:
        text = capture_help()
        assert "defaults (structure)." in text
        assert "Opt-in groups: calls, types, imports, io," in text

    def test_the_table(self, only_structure_by_default: None) -> None:
        rows = {row[0]: row for row in _rows(format_capture_groups_table())}
        assert rows["`structure`"][1] != "-"
        assert rows["`calls`"][1] == "-"

    def test_every_group_has_a_description(self) -> None:
        assert set(cs.CAPTURE_GROUP_SUMMARIES) == set(cs.CaptureGroup)


class TestTheSchemaTablesPointAtTheirGroup:
    """Each row a default index leaves out names its group; the rest are left
    as they were."""

    def test_every_opt_in_label_names_its_group(self, schema_page: str) -> None:
        owner = {
            label.value: group
            for group, labels in cs.CAPTURE_GROUP_NODE_LABELS.items()
            for label in labels
        }
        defaults = _resolver_defaults()
        rows = _rows(_section(schema_page, "node_schemas"))
        for row in rows:
            label, noted = _opt_in_note(row[0])
            group = owner.get(label)
            expected = group if group and group not in defaults else None
            assert noted == expected, row
        assert any(_opt_in_note(row[0])[1] for row in rows)

    def test_every_opt_in_relationship_names_its_group(self, schema_page: str) -> None:
        owner = {
            rel.value: group
            for group, rels in cs.CAPTURE_GROUP_RELS.items()
            for rel in rels
        }
        defaults = _resolver_defaults()
        rows = _rows(_section(schema_page, "relationship_schemas"))
        for row in rows:
            rel, noted = _opt_in_note(row[1])
            group = owner[rel]
            assert noted == (group if group not in defaults else None), row
        assert any(_opt_in_note(row[1])[1] for row in rows)


class TestCgrCaptureIsDocumented:
    def test_the_configuration_page_has_a_row(self) -> None:
        page = CONFIG_PAGE.read_text(encoding="utf-8")
        row = next(
            (
                line
                for line in page.splitlines()
                if line.startswith("| `CGR_CAPTURE` |")
            ),
            None,
        )
        assert row, "no CGR_CAPTURE row in configuration.md"
        assert f"../architecture/{CAPTURE_GROUPS_LINK}" in row

    def test_the_env_example_mentions_it(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert re.search(r"^# CGR_CAPTURE=", text, re.MULTILINE)
        assert "graph-schema.md#capture-groups" in text

    def test_the_readme_points_at_the_groups(self) -> None:
        assert f"docs/architecture/{CAPTURE_GROUPS_LINK}" in README.read_text(
            encoding="utf-8"
        )

    def test_the_data_flow_page_links_to_the_groups(self) -> None:
        assert CAPTURE_GROUPS_LINK in DATA_FLOW_PAGE.read_text(encoding="utf-8")


class TestTheDefaultSelectionIsUnchanged:
    """Negative: documenting the defaults must not move them."""

    def test_the_default_groups(self) -> None:
        assert cs.DEFAULT_CAPTURE_GROUPS == {
            cs.CaptureGroup.STRUCTURE,
            cs.CaptureGroup.CALLS,
            cs.CaptureGroup.TYPES,
            cs.CaptureGroup.IMPORTS,
        }

    def test_an_empty_selection_resolves_to_them(self) -> None:
        selection = resolve_capture([])
        expected = frozenset().union(
            *(cs.CAPTURE_GROUP_RELS[g] for g in cs.DEFAULT_CAPTURE_GROUPS)
        )
        assert selection.enabled_rels == expected
        for group, labels in cs.CAPTURE_GROUP_NODE_LABELS.items():
            assert all(not selection.node_enabled(label) for label in labels), group

    def test_an_unset_cgr_capture_is_the_default(self, no_env_capture: None) -> None:
        assert cgr_cli._capture_selection(None) == resolve_capture([])


class TestTokensResolveAsBefore:
    """Negative: the resolver and the option are untouched."""

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            (["io"], cs.DEFAULT_CAPTURE_GROUPS | {cs.CaptureGroup.IO}),
            (["none", "structure"], {cs.CaptureGroup.STRUCTURE}),
            (["all"], set(cs.CaptureGroup)),
            (["none"], set()),
            (["-calls"], cs.DEFAULT_CAPTURE_GROUPS - {cs.CaptureGroup.CALLS}),
            # A name that is both a group and a relationship type is the
            # group: `-CALLS` also drops REFERENCES and INSTANTIATES.
            (["-CALLS"], cs.DEFAULT_CAPTURE_GROUPS - {cs.CaptureGroup.CALLS}),
            (["+Parameters"], cs.DEFAULT_CAPTURE_GROUPS | {cs.CaptureGroup.PARAMETERS}),
            (["all", "-io"], set(cs.CaptureGroup) - {cs.CaptureGroup.IO}),
        ],
    )
    def test_group_tokens(
        self, tokens: list[str], expected: set[cs.CaptureGroup]
    ) -> None:
        rels = frozenset().union(*(cs.CAPTURE_GROUP_RELS[g] for g in expected))
        assert resolve_capture(tokens).enabled_rels == rels

    def test_a_relationship_type_token(self) -> None:
        selection = resolve_capture(["none", "+OVERRIDES", "has_parameter"])
        assert selection.enabled_rels == {
            cs.RelationshipType.OVERRIDES,
            cs.RelationshipType.HAS_PARAMETER,
        }
        # A group's label follows any one of its relationships.
        assert selection.node_enabled(cs.NodeLabel.PARAMETER)

    @pytest.mark.parametrize("token", ["strcuture", "+Parameter", "-nope"])
    def test_an_unknown_token_is_skipped_with_the_same_warning(
        self, token: str
    ) -> None:
        assert _warnings([token]) == [f"Ignoring unknown capture token: {token}"]
        assert resolve_capture([token]) == resolve_capture([])

    def test_the_variable_splits_on_commas_semicolons_and_spaces(self) -> None:
        assert split_spec("io,findings; parameters  -calls") == [
            "io",
            "findings",
            "parameters",
            "-calls",
        ]

    def test_the_flag_applies_after_the_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_CAPTURE", "io")
        assert cgr_cli._capture_selection(None) == resolve_capture(["io"])
        assert cgr_cli._capture_selection(["none"]).enabled_rels == frozenset()

    @pytest.mark.parametrize("command", CAPTURE_COMMANDS)
    def test_the_option_is_still_a_repeatable_string(self, command: str) -> None:
        option = _option(command, "--capture")
        assert option.multiple
        assert option.type.name == "str"
        assert option.default is None
        assert not option.required


class TestOtherHelpIsUnaffected:
    """Negative: only the two `--capture` options carry the capture help."""

    def test_no_other_option_documents_capture_groups(self) -> None:
        carriers = {
            (path, flag)
            for path, command in _commands()
            for param in _options(command)
            if "CGR_CAPTURE" in (param.help or "")
            for flag in param.opts
        }
        assert carriers == {(c, "--capture") for c in CAPTURE_COMMANDS}

    @pytest.mark.parametrize("command", [(), ("stats",), ("export",), ("optimize",)])
    def test_other_help_pages_do_not_mention_them(
        self, command: tuple[str, ...]
    ) -> None:
        text = _help(*command)
        assert cs.CaptureGroup.ENUM_VARIANTS.value not in text
        assert "Opt-in groups" not in text

    def test_the_schema_rows_are_the_schema(self, schema_page: str) -> None:
        # The note is a pointer; the labels, properties and endpoints each row
        # documents stay the schema's own.
        nodes = _rows(_section(schema_page, "node_schemas"))
        assert [(_opt_in_note(r[0])[0], r[1]) for r in nodes] == [
            (s.label.value, f"`{s.properties}`") for s in NODE_SCHEMAS
        ]
        rels = _rows(_section(schema_page, "relationship_schemas"))
        assert [(r[0], _opt_in_note(r[1])[0], r[2]) for r in rels] == [
            (
                ", ".join(s.sources),
                s.rel_type.value,
                ", ".join(s.targets),
            )
            for s in RELATIONSHIP_SCHEMAS
        ]
