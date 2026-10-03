"""Issue #2650: a failed tool call reaches the MCP client with `isError: true`.

The low-level SDK wraps a returned content list in `CallToolResult(isError=
False)`, and the server returned every failure it detected itself that way:
an unknown tool, a path outside the project root, a `surgical_replace_code`
whose target was not found, a handler that raised. A client (an agent loop,
a retry policy, an error UI) could only tell by parsing prose, so a failed
edit read as a successful one.

These tests drive the real server through the SDK's in-memory client, so the
flag is checked where a client reads it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from codebase_rag import constants as cs
from codebase_rag.mcp import server as mcp_server
from codebase_rag.mcp.tools import MCPToolsRegistry

LIB = "def helper(a):\n    return a\n"

pytestmark = [pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lib.py").write_text(LIB, encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(root))
    return root


@pytest.fixture
def no_llm() -> Iterator[None]:
    # The tools under test never generate Cypher; the server only needs a
    # generator object to build its registry.
    with patch.object(mcp_server, "LazyCypherGenerator", MagicMock()):
        yield


async def _call(name: str, arguments: dict[str, object]) -> CallToolResult:
    server, _ingestor = mcp_server.create_server()
    async with create_connected_server_and_client_session(server) as client:
        return await client.call_tool(name, arguments)


def _text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


FAILURES = [
    pytest.param("no_such_tool", {}, "Unknown tool", id="unknown-tool"),
    pytest.param(
        cs.MCPToolName.READ_FILE,
        {"file_path": "../outside.txt"},
        "outside",
        id="read-outside-root",
    ),
    pytest.param(
        cs.MCPToolName.READ_FILE,
        {"file_path": "../outside.txt", "offset": 0},
        "outside",
        id="read-slice-outside-root",
    ),
    pytest.param(
        cs.MCPToolName.READ_FILE, {"file_path": "missing.py"}, "", id="read-missing"
    ),
    pytest.param(
        cs.MCPToolName.SURGICAL_REPLACE_CODE,
        {
            "file_path": "lib.py",
            "target_code": "def missing():\n    pass\n",
            "replacement_code": "def x():\n    pass\n",
        },
        "lib.py",
        id="surgical-target-not-found",
    ),
    pytest.param(
        cs.MCPToolName.WRITE_FILE,
        {"file_path": "../escape.py", "content": "x = 1\n"},
        "",
        id="write-outside-root",
    ),
    pytest.param(
        cs.MCPToolName.LIST_DIRECTORY,
        {"directory_path": ".."},
        "",
        id="list-outside-root",
    ),
]


@pytest.mark.parametrize(("tool", "arguments", "named"), FAILURES)
async def test_a_failed_call_is_flagged_as_an_error(
    repo: Path, no_llm: None, tool: str, arguments: dict[str, object], named: str
) -> None:
    result = await _call(tool, arguments)

    assert result.isError is True, _text(result)
    assert named in _text(result)


async def test_a_failed_edit_leaves_the_file_as_it_was(
    repo: Path, no_llm: None
) -> None:
    await _call(
        cs.MCPToolName.SURGICAL_REPLACE_CODE,
        {
            "file_path": "lib.py",
            "target_code": "def missing():\n    pass\n",
            "replacement_code": "def x():\n    pass\n",
        },
    )

    assert (repo / "lib.py").read_text(encoding="utf-8") == LIB
    assert not (repo.parent / "escape.py").exists()


async def test_a_handler_that_raises_is_flagged_as_an_error(
    repo: Path, no_llm: None
) -> None:
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(MCPToolsRegistry, "list_directory", boom):
        result = await _call(cs.MCPToolName.LIST_DIRECTORY, {})

    assert result.isError is True
    assert "boom" in _text(result)


# Negative: what must not change.


async def test_reading_a_file_is_no_error(repo: Path, no_llm: None) -> None:
    result = await _call(cs.MCPToolName.READ_FILE, {"file_path": "lib.py"})

    assert result.isError is False
    assert _text(result) == LIB


async def test_listing_the_root_is_no_error(repo: Path, no_llm: None) -> None:
    result = await _call(cs.MCPToolName.LIST_DIRECTORY, {})

    assert result.isError is False
    assert "lib.py" in _text(result)
