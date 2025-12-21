from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import Tool
from rich.console import Console

from codebase_rag.errors import LLMGenerationError
from codebase_rag.tools.codebase_query import create_query_tool

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def mock_ingestor() -> MagicMock:
    ingestor = MagicMock()
    ingestor.fetch_all.return_value = [
        {"name": "func1", "type": "Function"},
        {"name": "func2", "type": "Method"},
    ]
    return ingestor


@pytest.fixture
def mock_cypher_gen() -> MagicMock:
    gen = MagicMock()
    gen.generate = AsyncMock(return_value="MATCH (n) RETURN n")
    return gen


@pytest.fixture
def mock_console() -> Console:
    return Console(force_terminal=False, no_color=True, width=80)


class TestCreateQueryTool:
    def test_creates_tool_instance(
        self, mock_ingestor: MagicMock, mock_cypher_gen: MagicMock
    ) -> None:
        tool = create_query_tool(mock_ingestor, mock_cypher_gen)
        assert isinstance(tool, Tool)

    def test_tool_has_description(
        self, mock_ingestor: MagicMock, mock_cypher_gen: MagicMock
    ) -> None:
        tool = create_query_tool(mock_ingestor, mock_cypher_gen)
        assert tool.description is not None
        assert "query" in tool.description.lower()
        assert "knowledge graph" in tool.description.lower()

    def test_creates_default_console(
        self, mock_ingestor: MagicMock, mock_cypher_gen: MagicMock
    ) -> None:
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=None)
        assert tool is not None

    def test_uses_provided_console(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        assert tool is not None


class TestQueryCodebaseKnowledgeGraph:
    async def test_successful_query_returns_results(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find all functions")
        assert result.results is not None
        assert len(result.results) == 2
        assert result.query_used == "MATCH (n) RETURN n"
        assert "2" in result.summary

    async def test_query_calls_cypher_generator(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        await tool.function(natural_language_query="Show me all classes")
        mock_cypher_gen.generate.assert_called_once_with("Show me all classes")

    async def test_query_calls_ingestor_fetch_all(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        await tool.function(natural_language_query="Find functions")
        mock_ingestor.fetch_all.assert_called_once_with("MATCH (n) RETURN n")

    async def test_empty_results_returns_zero_count(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_ingestor.fetch_all.return_value = []
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find nonexistent")
        assert result.results == []
        assert "0" in result.summary

    async def test_llm_generation_error_handled(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_cypher_gen.generate = AsyncMock(
            side_effect=LLMGenerationError("Generation failed")
        )
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Invalid query")
        assert result.results == []
        assert (
            "translation failed" in result.summary.lower()
            or "generation failed" in result.summary.lower()
        )

    async def test_database_error_handled(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_ingestor.fetch_all.side_effect = Exception("Database connection failed")
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find functions")
        assert result.results == []
        assert "error" in result.summary.lower()


class TestQueryResultFormatting:
    async def test_result_contains_query_used(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_cypher_gen.generate = AsyncMock(
            return_value="MATCH (f:Function) RETURN f.name"
        )
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find functions")
        assert result.query_used == "MATCH (f:Function) RETURN f.name"

    async def test_result_summary_contains_count(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"name": "a"},
            {"name": "b"},
            {"name": "c"},
        ]
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find all")
        assert "3" in result.summary


class TestQueryWithVariousDataTypes:
    async def test_handles_none_values(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"name": "func1", "description": None},
        ]
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find functions")
        assert len(result.results) == 1

    async def test_handles_boolean_values(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"name": "func1", "is_async": True},
            {"name": "func2", "is_async": False},
        ]
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find async functions")
        assert len(result.results) == 2

    async def test_handles_numeric_values(
        self,
        mock_ingestor: MagicMock,
        mock_cypher_gen: MagicMock,
        mock_console: Console,
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"name": "func1", "line_count": 42, "complexity": 3.14},
        ]
        tool = create_query_tool(mock_ingestor, mock_cypher_gen, console=mock_console)
        result = await tool.function(natural_language_query="Find complex functions")
        assert len(result.results) == 1
