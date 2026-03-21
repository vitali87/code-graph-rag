from __future__ import annotations

from codebase_rag.types_defs import ResultRow
from codebase_rag.utils.token_utils import count_tokens, truncate_results_by_tokens


class TestCountTokens:
    def test_empty_string(self) -> None:
        assert count_tokens("") == 0

    def test_simple_string(self) -> None:
        tokens = count_tokens("hello world")
        assert tokens > 0

    def test_longer_string_has_more_tokens(self) -> None:
        short = count_tokens("hello")
        long = count_tokens("hello world this is a longer string with more tokens")
        assert long > short


class TestTruncateResultsByTokens:
    def test_empty_results(self) -> None:
        results, tokens, truncated = truncate_results_by_tokens([], max_tokens=1000)
        assert results == []
        assert tokens == 0
        assert truncated is False

    def test_results_within_limit(self) -> None:
        rows: list[ResultRow] = [
            {"name": "foo", "count": 1},
            {"name": "bar", "count": 2},
        ]
        results, tokens, truncated = truncate_results_by_tokens(rows, max_tokens=10000)
        assert len(results) == 2
        assert tokens > 0
        assert truncated is False

    def test_results_exceed_limit(self) -> None:
        rows: list[ResultRow] = [
            {"name": f"function_{i}", "path": f"src/module_{i}/file_{i}.py"}
            for i in range(100)
        ]
        results, tokens, truncated = truncate_results_by_tokens(rows, max_tokens=200)
        assert len(results) < 100
        assert len(results) > 0
        assert tokens <= 200
        assert truncated is True

    def test_single_large_row_still_included(self) -> None:
        rows: list[ResultRow] = [
            {"content": "x" * 5000},
        ]
        results, tokens, truncated = truncate_results_by_tokens(rows, max_tokens=10)
        assert len(results) == 1
        assert truncated is False

    def test_preserves_row_order(self) -> None:
        rows: list[ResultRow] = [
            {"name": "first"},
            {"name": "second"},
            {"name": "third"},
        ]
        results, _, _ = truncate_results_by_tokens(rows, max_tokens=10000)
        assert [r["name"] for r in results] == ["first", "second", "third"]

    def test_token_count_accuracy(self) -> None:
        rows: list[ResultRow] = [
            {"name": "hello world"},
        ]
        results, tokens, _ = truncate_results_by_tokens(rows, max_tokens=10000)
        assert tokens == count_tokens('{"name": "hello world"}')
