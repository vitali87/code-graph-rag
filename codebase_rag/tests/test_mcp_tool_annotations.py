"""Issue #3458: MCP tools say whether they write, and a read-only server
serves only the readers.

`list_tools` sent every tool with a name, a description and a schema, so a
client could not tell `callers` from `wipe_database`, and a client that only
needed queries still got every write tool.

The server is driven through the SDK's in-memory client, so annotations and
refusals are checked where a client reads them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent, Tool
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.mcp import server as mcp_server

pytestmark = [pytest.mark.anyio]

# Spelled out rather than derived, so adding a tool fails here until someone
# decides whether it writes.
WRITERS = frozenset(
    {
        cs.MCPToolName.DELETE_PROJECT,
        cs.MCPToolName.WIPE_DATABASE,
        cs.MCPToolName.INDEX_REPOSITORY,
        cs.MCPToolName.UPDATE_REPOSITORY,
        cs.MCPToolName.REINGEST,
        cs.MCPToolName.RENAME,
        cs.MCPToolName.CHANGE_SIGNATURE,
        cs.MCPToolName.MOVE,
        cs.MCPToolName.SURGICAL_REPLACE_CODE,
        cs.MCPToolName.WRITE_FILE,
        cs.MCPToolName.STRUCTURAL_REPLACE,
        cs.MCPToolName.ANNOTATE,
    }
)
# The graph-driven edit operations, each called with arguments that would
# rewrite the repository if the server ran it.
EDIT_CALLS: list[tuple[cs.MCPToolName, dict[str, str | list[str]]]] = [
    (
        cs.MCPToolName.CHANGE_SIGNATURE,
        {"qualified_name": "proj.lib.helper", "new_params": ["a", "b"]},
    ),
    (
        cs.MCPToolName.MOVE,
        {"qualified_name": "proj.lib.helper", "target_module": "proj.other"},
    ),
]


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


@pytest.fixture
def read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MCP_READ_ONLY", True)


async def _tools() -> dict[str, Tool]:
    server, _ingestor = mcp_server.create_server()
    async with Client(server, mode="legacy") as client:
        listed = await client.list_tools()
    return {tool.name: tool for tool in listed.tools}


async def _call(name: str, arguments: dict[str, str | list[str]]) -> CallToolResult:
    server, _ingestor = mcp_server.create_server()
    async with Client(server, mode="legacy") as client:
        return await client.call_tool(name, arguments)


def _text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


def _files(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file()
    }


def test_every_tool_is_classified() -> None:
    assert frozenset(cs.MCPToolName) - cs.MCP_READ_ONLY_TOOLS == WRITERS
    assert cs.MCP_DESTRUCTIVE_TOOLS <= WRITERS
    assert cs.MCP_IDEMPOTENT_TOOLS <= WRITERS


async def test_every_listed_tool_says_whether_it_writes(
    repo: Path, no_llm: None
) -> None:
    tools = await _tools()

    # Every tool but semantic_search, which needs the `semantic` extra.
    assert set(tools) >= {str(name) for name in cs.MCPToolName} - {
        cs.MCPToolName.SEMANTIC_SEARCH
    }
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.annotations.read_only_hint is (name not in WRITERS), name
        assert tool.annotations.open_world_hint is False, name


async def test_the_destructive_tools_say_so(repo: Path, no_llm: None) -> None:
    tools = await _tools()

    for name in (
        cs.MCPToolName.WIPE_DATABASE,
        cs.MCPToolName.DELETE_PROJECT,
        cs.MCPToolName.WRITE_FILE,
        cs.MCPToolName.STRUCTURAL_REPLACE,
        cs.MCPToolName.CHANGE_SIGNATURE,
        cs.MCPToolName.MOVE,
    ):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.destructive_hint is True, name
    annotate = tools[cs.MCPToolName.ANNOTATE].annotations
    assert annotate is not None
    assert annotate.destructive_hint is False


async def test_a_read_only_server_lists_no_writer(
    repo: Path, no_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    everything = set(await _tools())
    monkeypatch.setattr(settings, "MCP_READ_ONLY", True)

    tools = set(await _tools())

    assert tools == everything - WRITERS
    assert tools >= {str(name) for name in cs.MCP_READ_ONLY_TOOLS} - {
        cs.MCPToolName.SEMANTIC_SEARCH
    }


async def test_a_read_only_server_refuses_a_writer_called_by_name(
    repo: Path, no_llm: None, read_only: None
) -> None:
    result = await _call(
        cs.MCPToolName.WRITE_FILE, {"file_path": "new.py", "content": "x = 1\n"}
    )

    assert result.is_error is True
    assert "read-only" in _text(result)
    assert not (repo / "new.py").exists()


@pytest.mark.parametrize(
    ("name", "arguments"), EDIT_CALLS, ids=[str(name) for name, _ in EDIT_CALLS]
)
async def test_a_read_only_server_refuses_an_edit_operation(
    repo: Path,
    no_llm: None,
    read_only: None,
    name: cs.MCPToolName,
    arguments: dict[str, str | list[str]],
) -> None:
    before = _files(repo)

    result = await _call(name, arguments)

    assert result.is_error is True
    assert "read-only" in _text(result)
    assert _files(repo) == before


async def test_a_read_only_server_still_reads(
    repo: Path, no_llm: None, read_only: None
) -> None:
    result = await _call(cs.MCPToolName.READ_FILE, {"file_path": "lib.py"})

    assert result.is_error is False
    assert "def helper" in _text(result)


def test_the_cli_flag_makes_the_server_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MCP_READ_ONLY", False)
    seen: list[bool] = []

    async def serve(workspace: str | None = None) -> None:
        seen.append(settings.MCP_READ_ONLY)

    with patch("codebase_rag.mcp.serve_stdio", AsyncMock(side_effect=serve)):
        result = CliRunner().invoke(app, ["mcp-server", "--read-only"])

    assert result.exit_code == 0, result.output
    assert seen == [True]
