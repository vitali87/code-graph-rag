"""Issue #2760: the MCP `reingest` tool's `deleted` parameter says what the
tool does with it.

It read "Files to remove from the graph even if a same-named file exists on
disk", but since #1799 a `deleted` path that is still a file is the
atomic-save race and is re-parsed, not removed (pinned by
`test_reingest_reports_a_present_deleted_path_as_reparsed_not_removed`). An
agent that wanted a file out of the graph was told the opposite of what
happens.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.types_defs import MCPInputSchema


@pytest.fixture
def reingest_schema(tmp_path: Path) -> MCPInputSchema:
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=MagicMock(), cypher_gen=MagicMock()
        )
    schemas = {s.name: s for s in registry.get_tool_schemas()}
    return schemas[cs.MCPToolName.REINGEST].inputSchema


def _deleted(schema: MCPInputSchema) -> str:
    return schema["properties"][cs.MCPParamName.DELETED]["description"]


def test_deleted_no_longer_promises_removal_of_a_file_on_disk(
    reingest_schema: MCPInputSchema,
) -> None:
    assert "even if a same-named file exists" not in _deleted(reingest_schema)


def test_deleted_says_a_file_on_disk_is_re_parsed(
    reingest_schema: MCPInputSchema,
) -> None:
    assert "re-parsed" in _deleted(reingest_schema)


def test_deleted_names_the_way_to_keep_a_file_out(
    reingest_schema: MCPInputSchema,
) -> None:
    assert ".cgrignore" in _deleted(reingest_schema)


# Negative: what must not change.


def test_the_tool_still_says_missing_files_are_removed() -> None:
    assert (
        "files that no longer exist are removed from the graph"
        in (td.MCP_TOOLS[cs.MCPToolName.REINGEST])
    )


def test_deleted_is_still_an_optional_list_of_paths(
    reingest_schema: MCPInputSchema,
) -> None:
    deleted = reingest_schema["properties"][cs.MCPParamName.DELETED]
    assert deleted["type"] == cs.MCPSchemaType.ARRAY
    assert reingest_schema["required"] == [cs.MCPParamName.PATHS]
