from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from codebase_rag.exceptions import LLMGenerationError
from codebase_rag.schemas import QueryGraphData
from codebase_rag.tools.codebase_query import create_query_tool

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def mock_ingestor_with_sample_data() -> MagicMock:
    ingestor = MagicMock()
    ingestor.fetch_all.return_value = [
        {"name": "hello_world", "type": "Function", "module": "main"},
        {"name": "Calculator", "type": "Class", "module": "math_utils"},
        {"name": "add", "type": "Method", "class": "Calculator"},
        {"name": "process_data", "type": "Function", "module": "data"},
    ]
    return ingestor


@pytest.fixture
def mock_cypher_gen_realistic() -> MagicMock:
    gen = MagicMock()

    async def generate_query(query: str) -> str:
        if "function" in query.lower():
            return "MATCH (f:Function) RETURN f.name as name, 'Function' as type"
        elif "class" in query.lower():
            return "MATCH (c:Class) RETURN c.name as name, 'Class' as type"
        else:
            return "MATCH (n) RETURN n.name as name, labels(n)[0] as type"

    gen.generate = generate_query
    return gen


@pytest.fixture
def silent_console() -> Console:
    return Console(force_terminal=False, no_color=True, width=80, quiet=True)


class TestQueryToolEndToEnd:
    async def test_complete_query_flow(
        self,
        mock_ingestor_with_sample_data: MagicMock,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        tool = create_query_tool(
            mock_ingestor_with_sample_data,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(
            natural_language_query="Find all functions in the codebase"
        )
        assert isinstance(result, QueryGraphData)
        assert len(result.results) == 4
        assert result.query_used is not None

    async def test_query_with_empty_results(
        self,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        empty_ingestor = MagicMock()
        empty_ingestor.fetch_all.return_value = []
        tool = create_query_tool(
            empty_ingestor,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(
            natural_language_query="Find something that doesn't exist"
        )
        assert result.results == []
        assert "0" in result.summary

    async def test_query_handles_llm_error_gracefully(
        self,
        mock_ingestor_with_sample_data: MagicMock,
        silent_console: Console,
    ) -> None:
        failing_gen = MagicMock()
        failing_gen.generate = AsyncMock(
            side_effect=LLMGenerationError("Model unavailable")
        )
        tool = create_query_tool(
            mock_ingestor_with_sample_data,
            failing_gen,
            console=silent_console,
        )
        result = await tool.function(natural_language_query="Any query")
        assert result.results == []
        assert "failed" in result.summary.lower() or "error" in result.summary.lower()

    async def test_query_handles_database_error_gracefully(
        self,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        failing_ingestor = MagicMock()
        failing_ingestor.fetch_all.side_effect = Exception("Connection refused")
        tool = create_query_tool(
            failing_ingestor,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(natural_language_query="Find functions")
        assert result.results == []
        assert "error" in result.summary.lower()


class TestQueryToolWithVariousInputs:
    async def test_query_about_functions(
        self,
        mock_ingestor_with_sample_data: MagicMock,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        tool = create_query_tool(
            mock_ingestor_with_sample_data,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(
            natural_language_query="What functions exist in the codebase?"
        )
        assert len(result.results) > 0

    async def test_query_about_classes(
        self,
        mock_ingestor_with_sample_data: MagicMock,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        tool = create_query_tool(
            mock_ingestor_with_sample_data,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(natural_language_query="Show me all classes")
        assert len(result.results) > 0

    async def test_query_with_unicode_characters(
        self,
        mock_ingestor_with_sample_data: MagicMock,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        tool = create_query_tool(
            mock_ingestor_with_sample_data,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(
            natural_language_query="Find functions with 世界 or émojis 🎉"
        )
        assert isinstance(result, QueryGraphData)


class TestQueryResultStructure:
    async def test_result_has_required_fields(
        self,
        mock_ingestor_with_sample_data: MagicMock,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        tool = create_query_tool(
            mock_ingestor_with_sample_data,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(natural_language_query="Find all")
        assert hasattr(result, "query_used")
        assert hasattr(result, "results")
        assert hasattr(result, "summary")

    async def test_result_preserves_data_types(
        self,
        mock_cypher_gen_realistic: MagicMock,
        silent_console: Console,
    ) -> None:
        typed_ingestor = MagicMock()
        typed_ingestor.fetch_all.return_value = [
            {"name": "test", "count": 42, "active": True, "ratio": 3.14},
        ]
        tool = create_query_tool(
            typed_ingestor,
            mock_cypher_gen_realistic,
            console=silent_console,
        )
        result = await tool.function(natural_language_query="Query")
        assert result.results[0]["count"] == 42
        assert result.results[0]["active"] is True
        assert result.results[0]["ratio"] == 3.14
