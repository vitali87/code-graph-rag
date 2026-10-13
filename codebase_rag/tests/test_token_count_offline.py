"""Issue #2914: counting tokens never needs the network.

`tiktoken.get_encoding("cl100k_base")` downloads the BPE file on first use.
With that host out of reach (an air-gapped or allow-listed network, the
documented local Ollama setup offline, a fresh `/tmp`), every natural-language
graph query failed after Memgraph had answered, reported as "There was an
error querying the database", and so did context pruning and slicing.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
import tiktoken
from rich.console import Console

from codebase_rag.mcp.tools import _plain_function
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.types_defs import ResultRow
from codebase_rag.utils import token_utils
from codebase_rag.utils.token_utils import count_tokens, truncate_results_by_tokens

OFFLINE = requests.exceptions.ProxyError(
    "HTTPSConnectionPool(host='openaipublic.blob.core.windows.net', port=443)"
)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def offline() -> Iterator[MagicMock]:
    token_utils._get_encoding.cache_clear()
    with patch.object(tiktoken, "get_encoding", side_effect=OFFLINE) as load:
        yield load
    token_utils._get_encoding.cache_clear()


class _Encoding:
    """An encoding that loaded: one token per character."""

    def encode_ordinary(self, text: str) -> list[int]:
        return [ord(c) for c in text]


@pytest.fixture
def online() -> Iterator[MagicMock]:
    token_utils._get_encoding.cache_clear()
    with patch.object(tiktoken, "get_encoding", return_value=_Encoding()) as load:
        yield load
    token_utils._get_encoding.cache_clear()


def test_tokens_are_estimated_when_the_encoding_cannot_load(
    offline: MagicMock,
) -> None:
    # About four bytes of UTF-8 a token, rounded up.
    assert count_tokens("") == 0
    assert count_tokens("abcd") == 1
    assert count_tokens("abcde") == 2
    assert count_tokens("é" * 4) == 2


def test_the_encoding_is_tried_once_and_the_miss_is_warned_once(
    offline: MagicMock,
) -> None:
    with patch.object(token_utils.logger, "warning") as warning:
        for text in ("one", "two", "three"):
            count_tokens(text)

    assert offline.call_count == 1
    assert warning.call_count == 1
    assert "TIKTOKEN_CACHE_DIR" in str(warning.call_args)


def test_results_are_still_truncated_by_the_estimate(offline: MagicMock) -> None:
    rows: list[ResultRow] = [{"name": "x" * 40}, {"name": "y" * 40}]

    kept, tokens, truncated = truncate_results_by_tokens(rows, max_tokens=15)

    assert kept == rows[:1]
    assert tokens == count_tokens('{"name": "' + "x" * 40 + '"}')
    assert truncated is True


@pytest.mark.anyio
async def test_a_graph_query_answers_offline(offline: MagicMock) -> None:
    ingestor = MagicMock()
    ingestor.fetch_read_only.return_value = [
        {"name": "func1", "type": "Function"},
        {"name": "func2", "type": "Method"},
    ]
    cypher_gen = MagicMock()
    cypher_gen.generate = AsyncMock(return_value="MATCH (n) RETURN n")
    console = Console(force_terminal=False, no_color=True, width=80)

    tool = create_query_tool(ingestor, cypher_gen, console=console)
    result = await _plain_function(tool)(natural_language_query="Find all functions")

    assert [row["name"] for row in result.results] == ["func1", "func2"]
    assert "error querying the database" not in result.summary


# Negative: what must not change.


def test_a_loaded_encoding_still_counts(online: MagicMock) -> None:
    assert count_tokens("abcdefgh") == 8
    assert online.call_count == 1
