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

from loguru import logger

from . import cli_help as ch
from .constants import (
    ENCODING_UTF8,
    LANGUAGE_METADATA,
    LanguageStatus,
    SupportedLanguage,
)
from .language_spec import LANGUAGE_SPECS
from .tools.tool_descriptions import AGENTIC_TOOLS, MCP_TOOLS
from .types_defs import NODE_SCHEMAS, RELATIONSHIP_SCHEMAS

PYPI_CACHE_FILE = Path(__file__).parent.parent / ".pypi_cache.json"
PYPI_CACHE_TTL_SECONDS = 86400
_PYPI_CACHE_LOCK = Lock()

CHECK_MARK = "\u2713"
DASH = "-"


class MakeCommand(NamedTuple):
    name: str
    description: str


MAKEFILE_PATTERN = re.compile(r"^([a-zA-Z_-]+):.*?## (.+)$")


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
    content = makefile_path.read_text()
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


def extract_node_schemas() -> list[tuple[str, str]]:
    return [(schema.label.value, schema.properties) for schema in NODE_SCHEMAS]


def format_node_schemas_table(schemas: list[tuple[str, str]]) -> str:
    rows = [[label, f"`{props}`"] for label, props in schemas]
    return format_markdown_table(["Label", "Properties"], rows)


def extract_relationship_schemas() -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for schema in RELATIONSHIP_SCHEMAS:
        sources = ", ".join(s.value for s in schema.sources)
        targets = ", ".join(t.value for t in schema.targets)
        result.append((sources, schema.rel_type.value, targets))
    return result


def format_relationship_schemas_table(schemas: list[tuple[str, str, str]]) -> str:
    rows = [[source, rel, target] for source, rel, target in schemas]
    return format_markdown_table(["Source", "Relationship", "Target"], rows)


def format_cli_commands_table() -> str:
    rows = [
        [f"`codebase-rag {cmd.value}`", desc] for cmd, desc in ch.CLI_COMMANDS.items()
    ]
    return format_markdown_table(["Command", "Description"], rows)


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
        data = json.loads(PYPI_CACHE_FILE.read_text())
        return {k: (v[0], v[1]) for k, v in data.items()}
    except (json.JSONDecodeError, KeyError, IndexError):
        return {}


def _save_pypi_cache(cache: dict[str, tuple[str, float]]) -> None:
    PYPI_CACHE_FILE.write_text(json.dumps({k: list(v) for k, v in cache.items()}))


def fetch_pypi_summary(package_name: str, cache: dict[str, tuple[str, float]]) -> str:
    now = time.time()
    with _PYPI_CACHE_LOCK:
        cached = cache.get(package_name)
        if cached and now - cached[1] < PYPI_CACHE_TTL_SECONDS:
            return cached[0]

    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            charset = response.headers.get_content_charset() or ENCODING_UTF8
            data = json.loads(response.read().decode(charset))
            summary = data.get("info", {}).get("summary", "") or ""
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning(f"Could not fetch PyPI summary for {package_name}: {e}")
        return ""

    with _PYPI_CACHE_LOCK:
        cache[package_name] = (summary, now)
    return summary


def format_dependencies(deps: list[str]) -> str:
    cache = _load_pypi_cache()
    with ThreadPoolExecutor() as executor:
        summaries = list(executor.map(lambda dep: fetch_pypi_summary(dep, cache), deps))
    lines: list[str] = []
    for name, summary in zip(deps, summaries):
        if summary:
            lines.append(f"- **{name}**: {summary}")
        else:
            lines.append(f"- **{name}**")
    _save_pypi_cache(cache)
    return "\n".join(lines)


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
        "cli_commands": format_cli_commands_table(),
        "mcp_tools": format_mcp_tools_table(),
        "agentic_tools": format_agentic_tools_table(),
        "dependencies": format_dependencies(deps),
    }
