from __future__ import annotations

import json
import re
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import NamedTuple

import typer
from loguru import logger
from typer.core import TyperCommand, TyperOption

from . import capture as cp
from . import cli_help as ch
from .cli import app as cli_app
from .constants import (
    ENCODING_UTF8,
    LANGUAGE_METADATA,
    CaptureGroup,
    LanguageStatus,
    SupportedLanguage,
)
from .language_spec import LANGUAGE_SPECS
from .tools.tool_descriptions import AGENTIC_TOOLS, MCP_TOOLS
from .types_defs import NODE_SCHEMAS, RELATIONSHIP_SCHEMAS

PYPI_CACHE_FILE = Path(__file__).parent.parent / ".pypi_cache.json"
PYPI_CACHE_TTL_SECONDS = 86400
_PYPI_CACHE_LOCK = Lock()
# The committed doc carrying the `dependencies` section. Its summaries are the
# fallback when PyPI cannot be reached, so an offline or throttled run (a CI
# runner, a laptop on a train) regenerates the same text instead of silently
# dropping every summary and reporting the section as stale.
DEPENDENCIES_DOC = Path("docs") / "getting-started" / "installation.md"
DEPENDENCY_LINE_PATTERN = re.compile(
    r"^- \*\*(?P<name>[^*]+)\*\*: (?P<summary>.+)$", re.MULTILINE
)

CHECK_MARK = "\u2713"
DASH = "-"
# Marks a schema-table row a default index leaves out and points it at its
# group, under the `## Capture Groups` heading of graph-schema.md (#2584).
# Only those rows change, so a concurrent edit to any other row still merges.
CAPTURE_OPT_IN_NOTE = " (opt-in: [`{group}`](#capture-groups))"

# Commands whose option table in docs/guide/cli-reference.md is generated from
# their Click parameters. The tables were hand-written and drifted from
# `--help` until `cgr start` documented 8 of its 21 options (#2426).
CLI_OPTION_COMMANDS = (
    ch.CLICommandName.START,
    ch.CLICommandName.INDEX,
    ch.CLICommandName.EXPORT,
    ch.CLICommandName.OPTIMIZE,
    ch.CLICommandName.MCP_SERVER,
    ch.CLICommandName.STATS,
    ch.CLICommandName.DEAD_CODE,
    ch.CLICommandName.DUPLICATES,
)
CLI_OPTIONS_SECTION = "cli_options_{name}"
SECTION_NAME_SEPARATOR = "_"
LONG_FLAG_PREFIX = "--"
FLAG_SEPARATOR = ", "
SECONDARY_FLAG_SEPARATOR = " / "
CODE_SPAN_DELIMITER = "`"
NOT_A_TYPER_COMMAND = "cgr {name} is not a TyperCommand, so it has no option table"
# Help text is written for a terminal. In a markdown table cell `<repo>` would
# parse as an HTML tag and `'*/tests/*'` as emphasis.
MARKDOWN_ESCAPES = (("<", "&lt;"), ("*", "\\*"))


class MakeCommand(NamedTuple):
    name: str
    description: str


MAKEFILE_PATTERN = re.compile(r"^([a-zA-Z_-]+):(?:(?!## ).)*## (.+)$")


def format_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    esc_headers = [str(h).replace("|", "\\|") for h in headers]
    esc_rows = [[str(cell).replace("|", "\\|") for cell in row] for row in rows]
    separator = "|".join("-" * max(len(h), 3) for h in esc_headers)
    lines = [
        "| " + " | ".join(esc_headers) + " |",
        "|" + separator + "|",
    ]
    for row in esc_rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def extract_makefile_commands(makefile_path: Path) -> list[MakeCommand]:
    commands: list[MakeCommand] = []
    content = makefile_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        if match := MAKEFILE_PATTERN.match(line):
            commands.append(
                MakeCommand(name=match.group(1), description=match.group(2))
            )
    return commands


def format_makefile_table(commands: list[MakeCommand]) -> str:
    rows = [[f"`make {cmd.name}`", cmd.description] for cmd in commands]
    return format_markdown_table(["Command", "Description"], rows)


def format_full_languages_table() -> str:
    headers = [
        "Language",
        "Status",
        "Extensions",
        "Functions",
        "Classes/Structs",
        "Modules",
        "Package Detection",
        "Additional Features",
    ]
    sorted_langs = sorted(
        SupportedLanguage,
        key=lambda lang: (
            LANGUAGE_METADATA[lang].status != LanguageStatus.FULL,
            lang.value,
        ),
    )
    rows: list[list[str]] = []
    for lang in sorted_langs:
        spec = LANGUAGE_SPECS[lang]
        meta = LANGUAGE_METADATA[lang]
        rows.append(
            [
                meta.display_name,
                meta.status.value,
                ", ".join(spec.file_extensions),
                CHECK_MARK if spec.function_node_types else DASH,
                CHECK_MARK if spec.class_node_types else DASH,
                CHECK_MARK if spec.module_node_types else DASH,
                CHECK_MARK if spec.package_indicators else DASH,
                meta.additional_features,
            ]
        )
    return format_markdown_table(headers, rows)


def capture_opt_in_note(group: CaptureGroup | None) -> str:
    if group is None or group in cp.default_groups():
        return ""
    return CAPTURE_OPT_IN_NOTE.format(group=group)


def extract_node_schemas() -> list[tuple[str, str]]:
    return [
        (
            schema.label.value + capture_opt_in_note(cp.node_label_group(schema.label)),
            schema.properties,
        )
        for schema in NODE_SCHEMAS
    ]


def format_node_schemas_table(schemas: list[tuple[str, str]]) -> str:
    rows = [[label, f"`{props}`"] for label, props in schemas]
    return format_markdown_table(["Label", "Properties"], rows)


def extract_relationship_schemas() -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for schema in RELATIONSHIP_SCHEMAS:
        sources = ", ".join(s.value for s in schema.sources)
        targets = ", ".join(t.value for t in schema.targets)
        rel = schema.rel_type.value + capture_opt_in_note(
            cp.relationship_group(schema.rel_type)
        )
        result.append((sources, rel, targets))
    return result


def format_relationship_schemas_table(schemas: list[tuple[str, str, str]]) -> str:
    rows = [[source, rel, target] for source, rel, target in schemas]
    return format_markdown_table(["Source", "Relationship", "Target"], rows)


def format_capture_groups_table() -> str:
    defaults = cp.default_groups()
    rows = [
        [
            f"`{group}`",
            CHECK_MARK if group in defaults else DASH,
            ", ".join(cp.group_node_labels(group)) or DASH,
            ", ".join(cp.group_relationships(group)),
            cp.group_summary(group),
        ]
        for group in CaptureGroup
    ]
    return format_markdown_table(
        ["Group", "Default", "Node labels", "Relationships", "Description"], rows
    )


def format_cli_commands_table() -> str:
    rows = [[f"`cgr {cmd.value}`", desc] for cmd, desc in ch.CLI_COMMANDS.items()]
    return format_markdown_table(["Command", "Description"], rows)


def cli_options_section_name(command: ch.CLICommandName) -> str:
    # A section marker name is `\w+`, which a dashed command name is not.
    return CLI_OPTIONS_SECTION.format(
        name=command.value.replace(DASH, SECTION_NAME_SEPARATOR)
    )


def _markdown_text(text: str) -> str:
    # Even-numbered pieces lie outside code spans; a code span shows its
    # characters literally, so an escape there would be printed.
    pieces = text.split(CODE_SPAN_DELIMITER)
    for index in range(0, len(pieces), 2):
        for char, escaped in MARKDOWN_ESCAPES:
            pieces[index] = pieces[index].replace(char, escaped)
    return CODE_SPAN_DELIMITER.join(pieces)


def _code_flags(flags: list[str]) -> str:
    return FLAG_SEPARATOR.join(f"`{flag}`" for flag in flags)


def _option_flags(option: TyperOption) -> str:
    # Long names first, as `--help` lists them, whatever order they were
    # declared in (`-o, --output` and `--project-name, -n` both occur).
    primary = sorted(
        option.opts, key=lambda flag: not flag.startswith(LONG_FLAG_PREFIX)
    )
    flags = _code_flags(primary)
    if option.secondary_opts:
        flags += SECONDARY_FLAG_SEPARATOR + _code_flags(option.secondary_opts)
    return flags


def format_cli_options_table(command: TyperCommand) -> str:
    # Typer's own classes throughout, never the real Click's: a typer that
    # vendors Click as `typer._click`, as the locked one does, builds options
    # that are not `click.Option`, so filtering on that class found no option
    # at all and wrote every table empty, on every platform.
    context = typer.Context(command, info_name=command.name)
    rows: list[list[str]] = []
    # `--help` is not among `params` (Click adds it per context), and an
    # argument belongs to the usage line rather than the option table.
    for param in command.params:
        if not isinstance(param, TyperOption):
            continue
        # The option's own help record, so each row reads as `--help` does, with
        # its `[default: ...]` and `[required]` notes; a hidden option has no
        # record and stays out of the docs as it stays out of `--help`.
        record = param.get_help_record(context)
        if record is None:
            continue
        _, help_text = record
        rows.append([_option_flags(param), _markdown_text(" ".join(help_text.split()))])
    return format_markdown_table(["Option", "Description"], rows)


def cli_option_command(name: ch.CLICommandName) -> TyperCommand:
    command = typer.main.get_group(cli_app).commands[name]
    if not isinstance(command, TyperCommand):
        raise TypeError(NOT_A_TYPER_COMMAND.format(name=name))
    return command


def format_cli_option_sections() -> dict[str, str]:
    return {
        cli_options_section_name(name): format_cli_options_table(
            cli_option_command(name)
        )
        for name in CLI_OPTION_COMMANDS
    }


def format_language_mappings() -> str:
    sorted_langs = sorted(
        SupportedLanguage,
        key=lambda lang: (
            LANGUAGE_METADATA[lang].status != LanguageStatus.FULL,
            lang.value,
        ),
    )
    lines: list[str] = []
    for lang in sorted_langs:
        spec = LANGUAGE_SPECS[lang]
        meta = LANGUAGE_METADATA[lang]
        node_types = list(spec.function_node_types) + list(spec.class_node_types)
        if not node_types:
            continue
        formatted_types = ", ".join(f"`{t}`" for t in sorted(node_types))
        lines.append(f"- **{meta.display_name}**: {formatted_types}")
    return "\n".join(lines)


def format_mcp_tools_table() -> str:
    rows = [[f"`{name.value}`", desc] for name, desc in MCP_TOOLS.items()]
    return format_markdown_table(["Tool", "Description"], rows)


def format_agentic_tools_table() -> str:
    rows = [[f"`{name.value}`", desc] for name, desc in AGENTIC_TOOLS.items()]
    return format_markdown_table(["Tool", "Description"], rows)


def extract_dependencies(pyproject_path: Path) -> list[str]:
    content = pyproject_path.read_bytes()
    data = tomllib.loads(content.decode(ENCODING_UTF8))
    deps = data.get("project", {}).get("dependencies", [])
    return [re.split(r"[<>=!~\[]", dep)[0].strip() for dep in deps]


def _load_pypi_cache() -> dict[str, tuple[str, float]]:
    if not PYPI_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(PYPI_CACHE_FILE.read_text(encoding="utf-8"))
        return {k: (v[0], v[1]) for k, v in data.items()}
    except (json.JSONDecodeError, KeyError, IndexError):
        return {}


def _save_pypi_cache(cache: dict[str, tuple[str, float]]) -> None:
    PYPI_CACHE_FILE.write_text(
        json.dumps({k: list(v) for k, v in cache.items()}), encoding="utf-8"
    )


def fetch_pypi_summary(package_name: str, cache: dict[str, tuple[str, float]]) -> str:
    now = time.time()
    with _PYPI_CACHE_LOCK:
        cached = cache.get(package_name)
        if cached and now - cached[1] < PYPI_CACHE_TTL_SECONDS:
            return cached[0]

    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - fixed https://pypi.org URL
            charset = response.headers.get_content_charset() or ENCODING_UTF8
            data = json.loads(response.read().decode(charset))
            summary = data.get("info", {}).get("summary", "") or ""
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning(f"Could not fetch PyPI summary for {package_name}: {e}")
        return ""

    with _PYPI_CACHE_LOCK:
        cache[package_name] = (summary, now)
    return summary


def committed_dependency_summaries(doc_path: Path) -> dict[str, str]:
    """Package -> summary as last written into the committed doc."""
    if not doc_path.exists():
        return {}
    content = doc_path.read_text(encoding=ENCODING_UTF8)
    return {
        m.group("name"): m.group("summary")
        for m in DEPENDENCY_LINE_PATTERN.finditer(content)
    }


def format_dependencies(
    deps: list[str], fallback_summaries: dict[str, str] | None = None
) -> str:
    # A failed fetch returns "" (fetch_pypi_summary logs and swallows the
    # network error); fall back to the committed summary rather than
    # rendering a bare name that makes the section look stale.
    fallback = fallback_summaries or {}
    cache = _load_pypi_cache()
    try:
        with ThreadPoolExecutor() as executor:
            summaries = list(
                executor.map(lambda dep: fetch_pypi_summary(dep, cache), deps)
            )
        lines: list[str] = []
        for name, fetched in zip(deps, summaries):
            summary = fetched or fallback.get(name, "")
            if summary:
                lines.append(f"- **{name}**: {summary}")
            else:
                lines.append(f"- **{name}**")
        return "\n".join(lines)
    finally:
        _save_pypi_cache(cache)


# Duplicated from scripts/update_news.py, which must stay stdlib-only and
# cannot depend on this package; a test asserts the two literals never drift.
LATEST_RELEASE_MARKER = "<!-- latest-release-end -->"


def _news_entries(content: str) -> tuple[list[str], int | None]:
    """Every "- " entry with its wrapped lines, and how many precede the marker.

    A blank line closes an entry (Markdown list-item semantics), so trailing
    prose or a header list elsewhere in the file is not swept into the news
    bullets. The latest-release marker also closes the entry above it and
    records how many entries belong to the latest release; only the FIRST
    marker counts, since NEWS.md accumulates one per release.
    """
    bullets: list[str] = []
    current: list[str] = []
    marker_count: int | None = None

    def close() -> None:
        nonlocal current
        if current:
            bullets.append("\n".join(current))
            current = []

    for line in content.splitlines():
        if line.strip() == LATEST_RELEASE_MARKER:
            close()
            if marker_count is None:
                marker_count = len(bullets)
        elif line.startswith("- "):
            close()
            current = [line]
        elif current and line.strip():
            current.append(line)
        else:
            close()
    close()
    return bullets, marker_count


def format_latest_news(news_path: Path, limit: int = 3) -> str:
    # Render the latest release's bullet entries from NEWS.md into the
    # README's "Latest News" section. NEWS.md is the source of truth (newest
    # first): the release workflow prepends entries via scripts/update_news.py
    # and leaves the latest-release marker below the block it inserted, so
    # every highlight of the latest release is rendered, however many there
    # are (a fixed top three once hid two of the five v0.0.720 highlights).
    # Hand edits remain welcome between releases (issue #1146) and land above
    # the marker, so they render too. Without a marker, or with no entries
    # above it, fall back to the top `limit` entries.
    try:
        content = news_path.read_text(encoding=ENCODING_UTF8)
    except OSError:
        return ""
    bullets, marker_count = _news_entries(content)
    count = marker_count if marker_count else limit
    return "\n".join(bullets[:count])


def generate_all_sections(project_root: Path) -> dict[str, str]:
    makefile_commands = extract_makefile_commands(project_root / "Makefile")
    node_schemas = extract_node_schemas()
    rel_schemas = extract_relationship_schemas()
    deps = extract_dependencies(project_root / "pyproject.toml")

    return {
        "makefile_commands": format_makefile_table(makefile_commands),
        "supported_languages": format_full_languages_table(),
        "language_mappings": format_language_mappings(),
        "node_schemas": format_node_schemas_table(node_schemas),
        "relationship_schemas": format_relationship_schemas_table(rel_schemas),
        "capture_groups": format_capture_groups_table(),
        "cli_commands": format_cli_commands_table(),
        "mcp_tools": format_mcp_tools_table(),
        "agentic_tools": format_agentic_tools_table(),
        "dependencies": format_dependencies(
            deps, committed_dependency_summaries(project_root / DEPENDENCIES_DOC)
        ),
        "latest_news": format_latest_news(project_root / "NEWS.md"),
        **format_cli_option_sections(),
    }
