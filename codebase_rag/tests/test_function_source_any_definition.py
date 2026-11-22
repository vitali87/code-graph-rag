"""Issue #3677: `get_function_source` reads any definition, and says when not.

The lookup matched only a node a Module DEFINES directly, so a method (hung
off its class by DEFINES_METHOD) or a nested function (DEFINED by its parent)
read as "could not retrieve" although `definition` and `get_code_snippet`
returned its source. The failure also reached the client with `isError:
false`, so it could not be told from source text.

These tests index a small repository into an embedded graph and drive the
real MCP server through the SDK's in-memory client.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_dialects import DIALECT_SQLITE
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp import server as mcp_server
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor

SIGNER = """class Signer:
    def sign(self, value):
        return value + "."

    def unsign(self, value):
        return value[:-1]


def outer():
    def inner():
        return 1

    return inner


def want_bytes(s):
    return s.encode()
"""

PROJECT = "proj"
UNKNOWN_NODE_ID = 987654321

pytestmark = [pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Path]:
    # The line ending is written as bytes, so a Windows run indexes the same
    # file as any other: `definition` keeps a file's own endings, and text
    # mode would give it CRLF there. A test asks for CRLF by parametrizing
    # this fixture.
    line_ending = getattr(request, "param", "\n")
    root = tmp_path / PROJECT
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_bytes(b"")
    (root / "pkg" / "signer.py").write_bytes(
        SIGNER.replace("\n", line_ending).encode("utf-8")
    )
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_SQLITE)
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(root))
    parsers, queries = load_parsers()
    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    ) as ingestor:
        GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=PROJECT,
            skip_embeddings=True,
        ).run(force=True)
    # The tools under test never generate Cypher; the server only needs a
    # generator object to build its registry.
    with patch.object(mcp_server, "LazyCypherGenerator", MagicMock()):
        yield root


def _node_id(qualified_name: str) -> int:
    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    ) as ingestor:
        rows = ingestor.fetch_all(
            "MATCH (n) WHERE n.qualified_name = $qn RETURN id(n) AS id",
            {"qn": qualified_name},
        )
    (row,) = rows
    node_id = row["id"]
    assert isinstance(node_id, int)
    return node_id


async def _call(name: str, arguments: dict[str, object]) -> CallToolResult:
    server, ingestor = mcp_server.create_server()
    with ingestor:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool(name, arguments)


def _text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


async def _source_from(tool: str, field: str, arguments: dict[str, object]) -> str:
    answer = json.loads(_text(await _call(tool, arguments)))
    assert answer.get("found") is True, answer
    source = answer[field]
    assert isinstance(source, str)
    return source


@pytest.mark.parametrize(
    "qualified_name",
    [
        pytest.param(f"{PROJECT}.pkg.signer.Signer.sign", id="method"),
        pytest.param(f"{PROJECT}.pkg.signer.outer.inner", id="nested-function"),
        pytest.param(f"{PROJECT}.pkg.signer.Signer", id="class"),
        pytest.param(f"{PROJECT}.pkg.signer.want_bytes", id="module-function"),
    ],
)
async def test_any_definition_returns_the_source_get_code_snippet_reads(
    repo: Path, qualified_name: str
) -> None:
    node_id = _node_id(qualified_name)

    result = await _call(cs.MCPToolName.GET_FUNCTION_SOURCE, {"node_id": node_id})

    assert result.is_error is False, _text(result)
    definition = await _source_from(
        cs.MCPToolName.DEFINITION,
        "source",
        {"qualified_name": qualified_name, "project": PROJECT},
    )
    snippet = await _source_from(
        cs.MCPToolName.GET_CODE_SNIPPET,
        "source_code",
        {"qualified_name": qualified_name},
    )
    # The same lines: `definition` and this tool trim the text's ends alike.
    assert definition == snippet.strip()
    assert _text(result) == cs.MSG_SEMANTIC_SOURCE_FORMAT.format(
        id=node_id, code=definition
    )


async def test_a_method_is_read_as_its_own_span(repo: Path) -> None:
    node_id = _node_id(f"{PROJECT}.pkg.signer.Signer.unsign")

    result = await _call(cs.MCPToolName.GET_FUNCTION_SOURCE, {"node_id": node_id})

    assert "def unsign(self, value):\n        return value[:-1]" in _text(result)
    assert "def sign" not in _text(result)


@pytest.mark.parametrize("repo", [pytest.param("\r\n", id="crlf")], indirect=True)
@pytest.mark.parametrize(
    "qualified_name",
    [
        pytest.param(f"{PROJECT}.pkg.signer.Signer.unsign", id="method"),
        pytest.param(f"{PROJECT}.pkg.signer.Signer", id="class"),
    ],
)
async def test_a_crlf_file_reads_as_get_code_snippet_reads_it(
    repo: Path, qualified_name: str
) -> None:
    # `get_code_snippet` reads in text mode, which folds "\r\n" to "\n"; the
    # same span of a CRLF checkout must read the same from both tools, on
    # every OS.
    node_id = _node_id(qualified_name)

    result = await _call(cs.MCPToolName.GET_FUNCTION_SOURCE, {"node_id": node_id})

    assert result.is_error is False, _text(result)
    snippet = await _source_from(
        cs.MCPToolName.GET_CODE_SNIPPET,
        "source_code",
        {"qualified_name": qualified_name},
    )
    assert "\r" not in snippet
    assert _text(result) == cs.MSG_SEMANTIC_SOURCE_FORMAT.format(
        id=node_id, code=snippet.strip()
    )


async def test_no_source_is_flagged_as_an_error(repo: Path) -> None:
    result = await _call(
        cs.MCPToolName.GET_FUNCTION_SOURCE, {"node_id": UNKNOWN_NODE_ID}
    )

    assert result.is_error is True
    assert _text(result) == cs.MSG_SEMANTIC_SOURCE_UNAVAILABLE.format(
        id=UNKNOWN_NODE_ID
    )
