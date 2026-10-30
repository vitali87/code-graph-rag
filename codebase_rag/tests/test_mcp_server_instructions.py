"""The MCP server tells a client when to use it, and its tool descriptions
open with what each tool does.

A client that defers tools behind a search (Claude Code's tool search) shows
its agent only `mcp__code-graph-rag__callers`, `__context`, `__resolve` and
so on. The server sent no `instructions`, so nothing said to reach for the
graph rather than grep, and some descriptions opened with a warning or with
other tools' names, which is what a keyword search matched on.

The server is driven through the SDK's in-memory client, so the instructions
are checked where a client reads them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcp import Client

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.mcp import server as mcp_server
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.types_defs import MCPInstructionHint

pytestmark = [pytest.mark.anyio]

_BACKTICKED = re.compile(r"`([a-z_]+)`")
_TOOL_NAMES = frozenset(str(name) for name in cs.MCPToolName)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lib.py").write_text("def helper(a):\n    return a\n", encoding="utf-8")
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(root))
    return root


@pytest.fixture
def no_llm() -> Iterator[None]:
    with patch.object(mcp_server, "LazyCypherGenerator", MagicMock()):
        yield


def _named_tools(text: str) -> set[str]:
    return set(_BACKTICKED.findall(text)) & _TOOL_NAMES


async def _connect() -> tuple[str | None, set[str]]:
    server, _ingestor = mcp_server.create_server()
    async with Client(server, mode="legacy") as client:
        listed = await client.list_tools()
        return client.instructions, {tool.name for tool in listed.tools}


async def test_the_server_sends_instructions(repo: Path, no_llm: None) -> None:
    instructions, _listed = await _connect()

    assert instructions is not None
    assert instructions.startswith(td.MCP_SERVER_INSTRUCTIONS)
    assert "grep" in instructions
    for name in (cs.MCPToolName.RESOLVE, cs.MCPToolName.CALLERS):
        assert f"`{name}`" in instructions


@pytest.mark.parametrize("read_only", [False, True])
async def test_instructions_name_only_listed_tools(
    repo: Path, no_llm: None, monkeypatch: pytest.MonkeyPatch, read_only: bool
) -> None:
    monkeypatch.setattr(settings, "MCP_READ_ONLY", read_only)

    instructions, listed = await _connect()

    assert instructions is not None
    assert _named_tools(instructions) <= listed


async def test_a_read_only_server_leaves_the_writers_out(
    repo: Path, no_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MCP_READ_ONLY", True)

    instructions, _listed = await _connect()

    assert instructions is not None
    named = _named_tools(instructions)
    assert not named - cs.MCP_READ_ONLY_TOOLS
    assert cs.MCPToolName.CALLERS in named
    assert cs.MCPToolName.RENAME not in named
    assert cs.MCPToolName.REINGEST not in named


@pytest.mark.parametrize(
    ("gate", "tool"),
    [
        ("has_semantic_dependencies", cs.MCPToolName.SEMANTIC_SEARCH),
        ("has_ast_grep", cs.MCPToolName.STRUCTURAL_SEARCH),
    ],
)
async def test_a_tool_missing_its_extra_is_not_offered(
    repo: Path, no_llm: None, gate: str, tool: cs.MCPToolName
) -> None:
    with patch(f"codebase_rag.mcp.tools.{gate}", return_value=False):
        instructions, listed = await _connect()

    assert tool not in listed
    assert instructions is not None
    assert f"`{tool}`" not in instructions
    # Needs no extra, so a line of its own survives the missing one.
    assert f"`{cs.MCPToolName.FIND_DUPLICATE_CODE}`" in instructions


@pytest.mark.parametrize("hint", td.MCP_SERVER_INSTRUCTION_HINTS)
def test_each_hint_declares_the_tools_it_names(hint: MCPInstructionHint) -> None:
    # The declared tools decide whether the line is sent, so a tool named in
    # the text and missing from them would be offered by a server without it.
    assert _named_tools(hint.text) == set(hint.tools)


def test_the_full_instructions_stay_short() -> None:
    # Sent into every session that has this server, used or not.
    full = td.MCP_SERVER_INSTRUCTIONS + "".join(
        td.MCP_SERVER_INSTRUCTIONS_BULLET + hint.text
        for hint in td.MCP_SERVER_INSTRUCTION_HINTS
    )
    assert len(full) <= 2048


@pytest.mark.parametrize("name", list(cs.MCPToolName))
def test_each_description_opens_with_what_the_tool_does(
    name: cs.MCPToolName,
) -> None:
    # Not a warning, and not a list of the other tools to use instead: the
    # opening is what an agent reads first in a search result.
    assert not td.MCP_TOOLS[name].startswith(("WARNING", "Prefer"))
