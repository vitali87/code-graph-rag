from __future__ import annotations

import pytest

from codebase_rag.services.graph_service import _apply_memory_limit


class TestApplyMemoryLimit:
    def test_appends_hint_to_simple_query(self) -> None:
        result = _apply_memory_limit("MATCH (n) RETURN n;", 4096)
        assert result == "MATCH (n) RETURN n QUERY MEMORY LIMIT 4096 MB;"

    def test_appends_hint_when_no_trailing_semicolon(self) -> None:
        result = _apply_memory_limit("MATCH (n) RETURN n", 256)
        assert result == "MATCH (n) RETURN n QUERY MEMORY LIMIT 256 MB;"

    def test_preserves_existing_hint(self) -> None:
        query = "MATCH (n) RETURN n QUERY MEMORY LIMIT 1024 MB;"
        assert _apply_memory_limit(query, 4096) == query

    def test_preserves_existing_hint_case_insensitive(self) -> None:
        query = "MATCH (n) RETURN n query memory limit 1024 mb;"
        assert _apply_memory_limit(query, 4096) == query

    def test_handles_trailing_whitespace(self) -> None:
        result = _apply_memory_limit("MATCH (n) RETURN n;\n  ", 4096)
        assert result == "MATCH (n) RETURN n QUERY MEMORY LIMIT 4096 MB;"

    def test_handles_whitespace_before_semicolon(self) -> None:
        result = _apply_memory_limit("MATCH (n) RETURN n  ;", 4096)
        assert result == "MATCH (n) RETURN n QUERY MEMORY LIMIT 4096 MB;"

    def test_handles_multiline_query(self) -> None:
        query = "MATCH (a)-[:CALLS*1..6]->(b)\nRETURN a, b;"
        result = _apply_memory_limit(query, 2048)
        assert result == (
            "MATCH (a)-[:CALLS*1..6]->(b)\nRETURN a, b QUERY MEMORY LIMIT 2048 MB;"
        )

    @pytest.mark.parametrize("mb", [128, 256, 1024, 4096, 16384])
    def test_uses_configured_megabytes(self, mb: int) -> None:
        result = _apply_memory_limit("MATCH (n) RETURN n;", mb)
        assert f"QUERY MEMORY LIMIT {mb} MB" in result
