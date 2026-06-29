from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.utils.dependencies import has_semantic_dependencies


@pytest.fixture
def mock_embed_code() -> MagicMock:
    mock = MagicMock()
    mock.return_value = [0.1] * 768
    return mock


@pytest.fixture
def mock_search_embeddings() -> MagicMock:
    mock = MagicMock()
    mock.return_value = [(1, 0.95), (2, 0.85), (3, 0.75)]
    return mock


@pytest.fixture
def mock_ingestor() -> MagicMock:
    mock = MagicMock()
    mock.fetch_all.return_value = [
        {
            "node_id": 1,
            "qualified_name": "project.module.func1",
            "name": "func1",
            "type": ["Function"],
        },
        {
            "node_id": 2,
            "qualified_name": "project.module.func2",
            "name": "func2",
            "type": ["Method"],
        },
        {
            "node_id": 3,
            "qualified_name": "project.module.func3",
            "name": "func3",
            "type": ["Function"],
        },
    ]
    return mock


def test_semantic_code_search_returns_empty_without_dependencies() -> None:
    if has_semantic_dependencies():
        pytest.skip("Dependencies are installed")

    from codebase_rag.tools.semantic_search import semantic_code_search

    results = semantic_code_search(MagicMock(), "find error handlers")
    assert results == []


@patch("codebase_rag.tools.semantic_search.has_semantic_dependencies", return_value=True)
@patch("codebase_rag.vector_store.search_embeddings")
@patch("codebase_rag.embedder.embed_code")
def test_semantic_code_search_reuses_injected_ingestor(
    mock_embed: MagicMock, mock_search: MagicMock, _deps: MagicMock
) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    mock_embed.return_value = [0.0]
    mock_search.return_value = [(1, 0.99), (2, 0.42)]

    ingestor = MagicMock()
    ingestor.fetch_all.return_value = [
        {
            "node_id": 1,
            "qualified_name": "pkg.mod.foo",
            "name": "foo",
            "type": ["Function"],
        },
        {
            "node_id": 2,
            "qualified_name": "pkg.mod.Bar",
            "name": "Bar",
            "type": ["Class"],
        },
    ]

    results = semantic_code_search(ingestor, "find the foo function", top_k=2)

    ingestor.fetch_all.assert_called_once()
    ingestor._execute_query.assert_not_called()
    assert [r["qualified_name"] for r in results] == ["pkg.mod.foo", "pkg.mod.Bar"]
    assert results[0]["score"] == 0.99


@patch("codebase_rag.tools.semantic_search.has_semantic_dependencies", return_value=True)
@patch("codebase_rag.vector_store.search_embeddings")
@patch("codebase_rag.embedder.embed_code")
def test_semantic_code_search_tolerates_missing_result_fields(
    mock_embed: MagicMock, mock_search: MagicMock, _deps: MagicMock
) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    mock_embed.return_value = [0.0]
    mock_search.return_value = [(1, 0.99)]

    ingestor = MagicMock()
    ingestor.fetch_all.return_value = [{"node_id": 1}]

    results = semantic_code_search(ingestor, "find foo", top_k=1)

    assert len(results) == 1
    assert results[0]["qualified_name"] == ""
    assert results[0]["name"] == ""
    assert results[0]["type"] == cs.SEMANTIC_TYPE_UNKNOWN


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_semantic_code_search_returns_formatted_results(
    mock_embed_code: MagicMock,
    mock_search_embeddings: MagicMock,
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    with (
        patch("codebase_rag.embedder.embed_code", mock_embed_code),
        patch("codebase_rag.vector_store.search_embeddings", mock_search_embeddings),
    ):
        results = semantic_code_search(
            mock_ingestor, "find authentication code", top_k=3
        )

    assert len(results) == 3
    assert results[0]["node_id"] == 1
    assert results[0]["qualified_name"] == "project.module.func1"
    assert results[0]["type"] == "Function"
    assert results[0]["score"] == 0.95


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_semantic_code_search_calls_embed_code_with_query(
    mock_embed_code: MagicMock,
    mock_search_embeddings: MagicMock,
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    with (
        patch("codebase_rag.embedder.embed_code", mock_embed_code),
        patch("codebase_rag.vector_store.search_embeddings", mock_search_embeddings),
    ):
        semantic_code_search(mock_ingestor, "database operations")

    mock_embed_code.assert_called_once_with("database operations")


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_semantic_code_search_passes_top_k_to_search(
    mock_embed_code: MagicMock,
    mock_search_embeddings: MagicMock,
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    with (
        patch("codebase_rag.embedder.embed_code", mock_embed_code),
        patch("codebase_rag.vector_store.search_embeddings", mock_search_embeddings),
    ):
        semantic_code_search(mock_ingestor, "file handling", top_k=10)

    mock_search_embeddings.assert_called_once_with([0.1] * 768, top_k=10)


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_semantic_code_search_returns_empty_when_no_matches(
    mock_embed_code: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    mock_search_empty = MagicMock(return_value=[])

    with (
        patch("codebase_rag.embedder.embed_code", mock_embed_code),
        patch("codebase_rag.vector_store.search_embeddings", mock_search_empty),
    ):
        results = semantic_code_search(MagicMock(), "nonexistent functionality")

    assert results == []


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_semantic_code_search_handles_exception(mock_embed_code: MagicMock) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    mock_embed_code.side_effect = Exception("Embedding failed")

    with patch("codebase_rag.embedder.embed_code", mock_embed_code):
        results = semantic_code_search(MagicMock(), "some query")

    assert results == []


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_semantic_code_search_preserves_score_order(
    mock_embed_code: MagicMock,
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    mock_search = MagicMock(return_value=[(3, 0.99), (1, 0.80), (2, 0.70)])

    with (
        patch("codebase_rag.embedder.embed_code", mock_embed_code),
        patch("codebase_rag.vector_store.search_embeddings", mock_search),
    ):
        results = semantic_code_search(mock_ingestor, "test query")

    assert results[0]["node_id"] == 3
    assert results[0]["score"] == 0.99
    assert results[1]["node_id"] == 1
    assert results[2]["node_id"] == 2


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_get_function_source_code_returns_source(mock_ingestor: MagicMock) -> None:
    from codebase_rag.tools.semantic_search import get_function_source_code

    mock_ingestor.fetch_all.return_value = [
        {
            "qualified_name": "project.module.func",
            "start_line": 10,
            "end_line": 15,
            "path": "/tmp/test.py",
        }
    ]

    mock_validate = MagicMock(return_value=(True, MagicMock()))
    mock_extract = MagicMock(return_value="def func():\n    return 42")

    with (
        patch(
            "codebase_rag.utils.source_extraction.validate_source_location",
            mock_validate,
        ),
        patch(
            "codebase_rag.utils.source_extraction.extract_source_lines", mock_extract
        ),
    ):
        result = get_function_source_code(mock_ingestor, 123)

    assert result == "def func():\n    return 42"


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_get_function_source_code_returns_none_when_not_found(
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import get_function_source_code

    mock_ingestor.fetch_all.return_value = []

    result = get_function_source_code(mock_ingestor, 999)

    assert result is None


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_get_function_source_code_returns_none_on_invalid_location(
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import get_function_source_code

    mock_ingestor.fetch_all.return_value = [
        {
            "qualified_name": "project.module.func",
            "start_line": None,
            "end_line": None,
            "path": None,
        }
    ]

    mock_validate = MagicMock(return_value=(False, None))

    with patch(
        "codebase_rag.utils.source_extraction.validate_source_location",
        mock_validate,
    ):
        result = get_function_source_code(mock_ingestor, 123)

    assert result is None


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_get_function_source_code_handles_exception(mock_ingestor: MagicMock) -> None:
    from codebase_rag.tools.semantic_search import get_function_source_code

    mock_ingestor.fetch_all.side_effect = Exception("Database error")

    result = get_function_source_code(mock_ingestor, 123)

    assert result is None


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_create_semantic_search_tool_returns_tool(mock_ingestor: MagicMock) -> None:
    from pydantic_ai import Tool

    from codebase_rag.tools.semantic_search import create_semantic_search_tool
    from codebase_rag.tools.tool_descriptions import AgenticToolName

    tool = create_semantic_search_tool(mock_ingestor)

    assert isinstance(tool, Tool)
    assert tool.name == AgenticToolName.SEMANTIC_SEARCH


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
def test_create_get_function_source_tool_returns_tool(mock_ingestor: MagicMock) -> None:
    from pydantic_ai import Tool

    from codebase_rag.tools.semantic_search import create_get_function_source_tool
    from codebase_rag.tools.tool_descriptions import AgenticToolName

    tool = create_get_function_source_tool(mock_ingestor)

    assert isinstance(tool, Tool)
    assert tool.name == AgenticToolName.GET_FUNCTION_SOURCE


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
@pytest.mark.asyncio
async def test_semantic_search_tool_formats_results(
    mock_embed_code: MagicMock,
    mock_search_embeddings: MagicMock,
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import create_semantic_search_tool

    tool = create_semantic_search_tool(mock_ingestor)

    with (
        patch("codebase_rag.embedder.embed_code", mock_embed_code),
        patch("codebase_rag.vector_store.search_embeddings", mock_search_embeddings),
    ):
        result = await tool.function("find handlers")

    assert "Found 3 semantic matches" in result
    assert "project.module.func1" in result
    assert "Function" in result
    assert "0.95" in result


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
@pytest.mark.asyncio
async def test_semantic_search_tool_handles_no_results(
    mock_embed_code: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import create_semantic_search_tool

    mock_search_empty = MagicMock(return_value=[])
    tool = create_semantic_search_tool(MagicMock())

    with (
        patch("codebase_rag.embedder.embed_code", mock_embed_code),
        patch("codebase_rag.vector_store.search_embeddings", mock_search_empty),
    ):
        result = await tool.function("nonexistent")

    assert "No semantic matches found" in result


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
@pytest.mark.asyncio
async def test_get_function_source_tool_returns_source(
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import create_get_function_source_tool

    mock_ingestor.fetch_all.return_value = [
        {
            "qualified_name": "project.func",
            "start_line": 1,
            "end_line": 3,
            "path": "/tmp/test.py",
        }
    ]

    mock_validate = MagicMock(return_value=(True, MagicMock()))
    mock_extract = MagicMock(return_value="def func(): pass")

    tool = create_get_function_source_tool(mock_ingestor)

    with (
        patch(
            "codebase_rag.utils.source_extraction.validate_source_location",
            mock_validate,
        ),
        patch(
            "codebase_rag.utils.source_extraction.extract_source_lines", mock_extract
        ),
    ):
        result = await tool.function(123)

    assert "Source code for node ID 123" in result
    assert "def func(): pass" in result


@pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic dependencies not installed"
)
@pytest.mark.asyncio
async def test_get_function_source_tool_handles_not_found(
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tools.semantic_search import create_get_function_source_tool

    mock_ingestor.fetch_all.return_value = []

    tool = create_get_function_source_tool(mock_ingestor)

    result = await tool.function(999)

    assert "Could not retrieve source code" in result
