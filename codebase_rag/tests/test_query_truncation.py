from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.types_defs import ResultRow


@pytest.fixture
def mock_ingestor() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_cypher_gen() -> MagicMock:
    gen = MagicMock()
    gen.generate = AsyncMock(return_value="MATCH (n) RETURN n")
    return gen


class TestQueryTruncation:
    @pytest.mark.asyncio
    async def test_row_cap_truncation(
        self, mock_ingestor: MagicMock, mock_cypher_gen: MagicMock
    ) -> None:
        rows: list[ResultRow] = [{"name": f"node_{i}"} for i in range(600)]
        mock_ingestor.fetch_all.return_value = rows

        tool = create_query_tool(mock_ingestor, mock_cypher_gen)
        with patch("codebase_rag.tools.codebase_query.settings") as mock_settings:
            mock_settings.QUERY_RESULT_ROW_CAP = 500
            mock_settings.QUERY_RESULT_MAX_TOKENS = 100000
            result = await tool.function(natural_language_query="list all nodes")

        assert len(result.results) <= 500
        assert "truncated" in result.summary.lower() or "600" in result.summary

    @pytest.mark.asyncio
    async def test_token_truncation(
        self, mock_ingestor: MagicMock, mock_cypher_gen: MagicMock
    ) -> None:
        rows: list[ResultRow] = [
            {"name": f"function_{i}", "body": f"def func_{i}(): pass  # {'x' * 200}"}
            for i in range(100)
        ]
        mock_ingestor.fetch_all.return_value = rows

        tool = create_query_tool(mock_ingestor, mock_cypher_gen)
        with patch("codebase_rag.tools.codebase_query.settings") as mock_settings:
            mock_settings.QUERY_RESULT_ROW_CAP = 500
            mock_settings.QUERY_RESULT_MAX_TOKENS = 500
            result = await tool.function(natural_language_query="list functions")

        assert len(result.results) < 100
        assert "truncated" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_no_truncation_when_within_limits(
        self, mock_ingestor: MagicMock, mock_cypher_gen: MagicMock
    ) -> None:
        rows: list[ResultRow] = [{"name": f"node_{i}"} for i in range(5)]
        mock_ingestor.fetch_all.return_value = rows

        tool = create_query_tool(mock_ingestor, mock_cypher_gen)
        with patch("codebase_rag.tools.codebase_query.settings") as mock_settings:
            mock_settings.QUERY_RESULT_ROW_CAP = 500
            mock_settings.QUERY_RESULT_MAX_TOKENS = 16000
            result = await tool.function(natural_language_query="small query")

        assert len(result.results) == 5
        assert "Successfully" in result.summary
