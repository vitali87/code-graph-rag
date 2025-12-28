from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from . import cli_help as ch
from .constants import SupportedLanguage
from .tools import tool_descriptions as td
from .types_defs import NODE_SCHEMAS, RELATIONSHIP_SCHEMAS


class MakeCommand(NamedTuple):
    name: str
    description: str


MAKEFILE_PATTERN = re.compile(r"^([a-zA-Z_-]+):.*?## (.+)$")

CLI_COMMANDS: list[tuple[str, str]] = [
    ("start", ch.CMD_START),
    ("index", ch.CMD_INDEX),
    ("export", ch.CMD_EXPORT),
    ("optimize", ch.CMD_OPTIMIZE),
    ("mcp-server", ch.CMD_MCP_SERVER),
    ("graph-loader", ch.CMD_GRAPH_LOADER),
    ("language", ch.CMD_LANGUAGE),
]

MCP_TOOL_MAPPING: dict[str, str] = {
    "index_repository": td.MCP_INDEX_REPOSITORY,
    "query_code_graph": td.MCP_QUERY_CODE_GRAPH,
    "get_code_snippet": td.MCP_GET_CODE_SNIPPET,
    "surgical_replace_code": td.MCP_SURGICAL_REPLACE_CODE,
    "read_file": td.MCP_READ_FILE,
    "write_file": td.MCP_WRITE_FILE,
    "list_directory": td.MCP_LIST_DIRECTORY,
}

AGENTIC_TOOL_MAPPING: dict[str, str] = {
    "query_graph": td.CODEBASE_QUERY,
    "read_file": td.FILE_READER,
    "create_file": td.FILE_WRITER,
    "replace_code": td.FILE_EDITOR,
    "list_directory": td.DIRECTORY_LISTER,
    "analyze_document": td.ANALYZE_DOCUMENT,
    "execute_shell": td.SHELL_COMMAND,
    "semantic_search": td.CODE_RETRIEVAL,
}


def format_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    separator = "|".join("-" * max(len(h), 3) for h in headers)
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + separator + "|",
    ]
    for row in rows:
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


def extract_supported_languages() -> list[str]:
    return [lang.value.title() for lang in SupportedLanguage]


def format_languages_table(languages: list[str]) -> str:
    rows = [[lang, "Full support"] for lang in languages]
    return format_markdown_table(["Language", "Status"], rows)


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
    rows = [[f"`codebase-rag {cmd}`", desc] for cmd, desc in CLI_COMMANDS]
    return format_markdown_table(["Command", "Description"], rows)


def format_mcp_tools_table() -> str:
    rows = [[f"`{name}`", desc] for name, desc in MCP_TOOL_MAPPING.items()]
    return format_markdown_table(["Tool", "Description"], rows)


def format_agentic_tools_table() -> str:
    rows = [[f"`{name}`", desc] for name, desc in AGENTIC_TOOL_MAPPING.items()]
    return format_markdown_table(["Tool", "Description"], rows)


def generate_all_sections(project_root: Path) -> dict[str, str]:
    makefile_commands = extract_makefile_commands(project_root / "Makefile")
    languages = extract_supported_languages()
    node_schemas = extract_node_schemas()
    rel_schemas = extract_relationship_schemas()

    return {
        "makefile_commands": format_makefile_table(makefile_commands),
        "supported_languages": format_languages_table(languages),
        "node_schemas": format_node_schemas_table(node_schemas),
        "relationship_schemas": format_relationship_schemas_table(rel_schemas),
        "cli_commands": format_cli_commands_table(),
        "mcp_tools": format_mcp_tools_table(),
        "agentic_tools": format_agentic_tools_table(),
    }
