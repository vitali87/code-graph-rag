"""A failing semantic search is reported as a failure, not as no matches.

Every vector-store backend, `semantic_code_search` and the source lookup
caught all exceptions and answered [] or None. The tools then said "no
semantic matches" or "the node may not exist", so an unreachable store, an
expired embedding key or a dimension mismatch reached the agent and MCP
clients as a successful empty answer, with the remediation in the error
message reduced to a log line.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag import tool_errors as te
from codebase_rag.tools.semantic_search import (
    create_get_function_source_tool,
    create_semantic_search_tool,
)
from codebase_rag.utils.dependencies import has_semantic_dependencies

_needs_semantic = pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)


@_needs_semantic
async def test_a_failing_vector_store_is_a_tool_failure() -> None:
    tool = create_semantic_search_tool(MagicMock())

    with (
        patch("codebase_rag.embedder.embed_code", return_value=[0.1] * 768),
        patch(
            "codebase_rag.vector_store.search_embeddings",
            side_effect=ValueError("dimension mismatch: re-index with --clean"),
        ),
    ):
        result = await tool.function("find handlers")

    assert isinstance(result, te.ToolFailure)
    assert "re-index with --clean" in result


@_needs_semantic
async def test_a_failing_embedder_is_a_tool_failure() -> None:
    tool = create_semantic_search_tool(MagicMock())

    with patch(
        "codebase_rag.embedder.embed_code", side_effect=RuntimeError("bad API key")
    ):
        result = await tool.function("find handlers")

    assert isinstance(result, te.ToolFailure)
    assert "bad API key" in result


async def test_missing_semantic_dependencies_are_a_tool_failure() -> None:
    tool = create_semantic_search_tool(MagicMock())

    with patch(
        "codebase_rag.tools.semantic_search.has_semantic_dependencies",
        return_value=False,
    ):
        result = await tool.function("find handlers")

    assert isinstance(result, te.ToolFailure)
    assert ex.SEMANTIC_EXTRA in result


@_needs_semantic
async def test_no_matches_is_still_a_plain_answer() -> None:
    tool = create_semantic_search_tool(MagicMock())

    with (
        patch("codebase_rag.embedder.embed_code", return_value=[0.1] * 768),
        patch("codebase_rag.vector_store.search_embeddings", return_value=[]),
    ):
        result = await tool.function("nothing like this")

    assert not isinstance(result, te.ToolFailure)
    assert result == cs.MSG_SEMANTIC_NO_RESULTS.format(query="nothing like this")


async def test_a_missing_node_is_a_tool_failure() -> None:
    ingestor = MagicMock()
    ingestor.fetch_all.return_value = []
    tool = create_get_function_source_tool(ingestor)

    result = await tool.function(999)

    assert isinstance(result, te.ToolFailure)
    assert result == cs.MSG_SEMANTIC_SOURCE_UNAVAILABLE.format(id=999)


async def test_a_failing_graph_read_is_reported_as_itself() -> None:
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = ConnectionError("graph unreachable")
    tool = create_get_function_source_tool(ingestor)

    result = await tool.function(123)

    assert isinstance(result, te.ToolFailure)
    assert "graph unreachable" in result
