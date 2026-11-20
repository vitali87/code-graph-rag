"""Tool descriptions and canned prompts must state their contract plainly.

A description is the only account of a tool the model gets: one that omits a
parameter, or names the wrong engine, sends it down paths no system prompt
can correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import Tool

from codebase_rag.constants import MCPToolName
from codebase_rag.prompts import (
    OPTIMIZATION_PROMPT,
    OPTIMIZATION_PROMPT_WITH_REFERENCE,
)
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool
from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
from codebase_rag.tools.file_writer import FileWriter, create_file_writer_tool
from codebase_rag.tools.shell_command import ShellCommander, create_shell_command_tool


def _file_and_shell_tools(root: Path) -> list[Tool]:
    return [
        create_directory_lister_tool(DirectoryLister(str(root))),
        create_file_reader_tool(FileReader(str(root))),
        create_file_writer_tool(FileWriter(str(root))),
        create_file_editor_tool(FileEditor(str(root))),
        create_shell_command_tool(ShellCommander(str(root))),
    ]


def test_file_and_shell_tool_descriptions_name_every_parameter(
    tmp_path: Path,
) -> None:
    """Each parameter the tool takes is named in its description, so the
    model reads what to pass next to what the tool does with it."""
    for tool in _file_and_shell_tools(tmp_path):
        params = tool.function_schema.json_schema["properties"]
        missing = [p for p in params if f"`{p}`" not in (tool.description or "")]
        assert not missing, f"{tool.name} does not name {missing}"


@pytest.mark.parametrize("name", list(td.MCP_TOOLS))
def test_mcp_descriptions_name_no_graph_engine(name: MCPToolName) -> None:
    """The MCP server serves both backends, so a description naming one
    engine is wrong on the other."""
    description = td.MCP_TOOLS[name]

    assert "Memgraph" not in description
    assert "Neo4j" not in description


@pytest.mark.parametrize(
    "template",
    [OPTIMIZATION_PROMPT, OPTIMIZATION_PROMPT_WITH_REFERENCE],
    ids=["plain", "with_reference"],
)
def test_optimization_prompts_state_the_approval_gate_once(template: str) -> None:
    """Propose first and change nothing until approved: said once, plainly.
    The prompt stated it twice, behind IMPORTANT and Remember markers."""
    text = template.format(language="python", reference_document="guide.pdf")

    assert "do not change any file until I approve" in text
    assert "IMPORTANT" not in text
    assert "Remember:" not in text
