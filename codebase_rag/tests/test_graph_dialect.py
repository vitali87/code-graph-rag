import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph.dialect import GraphDialect
from codebase_rag.services.graph.memgraph import MemgraphDialect
from codebase_rag.services.graph.retry import retry_on_transient


def test_memgraph_dialect_satisfies_protocol() -> None:
    assert isinstance(MemgraphDialect(), GraphDialect)


def test_memgraph_dialect_name() -> None:
    assert MemgraphDialect().name == GraphBackend.MEMGRAPH


def test_apply_query_limit_appends_suffix() -> None:
    out = MemgraphDialect().apply_query_limit("MATCH (n) RETURN n", 512)
    assert out == "MATCH (n) RETURN n QUERY MEMORY LIMIT 512 MB;"


def test_apply_query_limit_is_idempotent() -> None:
    once = MemgraphDialect().apply_query_limit("MATCH (n) RETURN n", 512)
    assert MemgraphDialect().apply_query_limit(once, 512) == once


def test_apply_query_limit_strips_trailing_semicolon_first() -> None:
    out = MemgraphDialect().apply_query_limit("MATCH (n) RETURN n;", 64)
    assert out == "MATCH (n) RETURN n QUERY MEMORY LIMIT 64 MB;"


def test_memgraph_is_retryable_is_always_false() -> None:
    # The retry loop must stay inert on the default backend.
    assert MemgraphDialect().is_retryable(RuntimeError("anything")) is False


def test_memgraph_is_benign_error_matches_already_exists() -> None:
    d = MemgraphDialect()
    assert d.is_benign_error(RuntimeError("Index already exists")) is True
    assert d.is_benign_error(RuntimeError("syntax error")) is False


def test_retry_on_transient_does_not_retry_when_dialect_says_no() -> None:
    calls = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        retry_on_transient(boom, MemgraphDialect(), attempts=3, base_delay=0.0)
    assert len(calls) == 1


def test_retry_on_transient_retries_then_succeeds() -> None:
    class AlwaysRetryDialect(MemgraphDialect):
        def is_retryable(self, exc: Exception) -> bool:
            return True

    calls = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("conflict")
        return "ok"

    assert (
        retry_on_transient(flaky, AlwaysRetryDialect(), attempts=5, base_delay=0.0)
        == "ok"
    )
    assert len(calls) == 3


def test_retry_on_transient_reraises_after_exhaustion() -> None:
    class AlwaysRetryDialect(MemgraphDialect):
        def is_retryable(self, exc: Exception) -> bool:
            return True

    def always_boom() -> None:
        raise RuntimeError("conflict")

    with pytest.raises(RuntimeError, match="conflict"):
        retry_on_transient(
            always_boom, AlwaysRetryDialect(), attempts=3, base_delay=0.0
        )
