from __future__ import annotations

import pytest

from codebase_rag.constants import (
    NODE_UNIQUE_CONSTRAINTS,
    GraphBackend,
    NodeLabel,
    RelationshipType,
)
from codebase_rag.services.graph.arcadedb import (
    ArcadeDBDialect,
    build_arcade_schema_statements,
)
from codebase_rag.services.graph.dialect import GraphDialect


def test_generates_one_vertex_type_per_node_label() -> None:
    stmts = build_arcade_schema_statements()
    for label in NodeLabel:
        assert f"CREATE VERTEX TYPE {label.value} IF NOT EXISTS" in stmts


def test_generates_one_edge_type_per_relationship_type() -> None:
    stmts = build_arcade_schema_statements()
    for rel in RelationshipType:
        assert f"CREATE EDGE TYPE {rel.value} IF NOT EXISTS" in stmts


def test_declares_the_unique_key_property_before_indexing_it() -> None:
    # ArcadeDB rejects CREATE INDEX on an undeclared property, so the
    # property statement must come first.
    stmts = build_arcade_schema_statements()
    prop = "CREATE PROPERTY Function.qualified_name IF NOT EXISTS STRING"
    index = "CREATE INDEX IF NOT EXISTS ON Function (qualified_name) UNIQUE"
    assert stmts.index(prop) < stmts.index(index)


def test_creates_the_vertex_type_before_its_property() -> None:
    stmts = build_arcade_schema_statements()
    assert stmts.index("CREATE VERTEX TYPE Function IF NOT EXISTS") < stmts.index(
        "CREATE PROPERTY Function.qualified_name IF NOT EXISTS STRING"
    )


def test_generates_a_unique_index_for_every_constrained_label() -> None:
    stmts = build_arcade_schema_statements()
    for label, key in NODE_UNIQUE_CONSTRAINTS.items():
        assert f"CREATE INDEX IF NOT EXISTS ON {label} ({key}) UNIQUE" in stmts


def test_statement_count_matches_the_constant_tables() -> None:
    # 20 vertex types + 20 properties + 20 unique indexes + 25 edge types.
    stmts = build_arcade_schema_statements()
    expected = len(NodeLabel) + len(NODE_UNIQUE_CONSTRAINTS) * 2 + len(RelationshipType)
    assert len(stmts) == expected


def test_every_statement_is_idempotent() -> None:
    for stmt in build_arcade_schema_statements():
        assert "IF NOT EXISTS" in stmt


def test_satisfies_the_dialect_protocol() -> None:
    assert isinstance(ArcadeDBDialect(), GraphDialect)


def test_name() -> None:
    assert ArcadeDBDialect().name == GraphBackend.ARCADEDB


def test_apply_query_limit_is_identity() -> None:
    # ArcadeDB has no QUERY MEMORY LIMIT equivalent; the read path bounds
    # wall clock with a transaction timeout instead.
    query = "MATCH (n) RETURN n"
    assert ArcadeDBDialect().apply_query_limit(query, 512) == query


def test_allowed_prefixes_narrow_to_algo() -> None:
    # `algo.` is already in the shared allowlist; the dialect narrows to it
    # rather than extending, because MAGE namespaces do not resolve here.
    assert ArcadeDBDialect().allowed_proc_prefixes == frozenset({"algo."})


@pytest.mark.parametrize(
    "message",
    [
        "Concurrent modification of record #12:3",
        "ConcurrentModificationException: cannot update",
        "TransientError: please retry",
    ],
)
def test_is_retryable_matches_write_conflicts(message: str) -> None:
    assert ArcadeDBDialect().is_retryable(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    ["Syntax error at line 1", "Unknown procedure/function: algo.nope"],
)
def test_is_retryable_rejects_permanent_errors(message: str) -> None:
    assert ArcadeDBDialect().is_retryable(RuntimeError(message)) is False


def test_is_benign_error_matches_already_exists() -> None:
    d = ArcadeDBDialect()
    assert d.is_benign_error(RuntimeError("Type 'Function' already exists")) is True
    assert d.is_benign_error(RuntimeError("Syntax error")) is False


def test_ensure_schema_sends_every_statement_over_http() -> None:
    sent: list[str] = []

    class _FakeHttp:
        def sql(self, command: str) -> list[dict[str, object]]:
            sent.append(command)
            return []

    ArcadeDBDialect(http=_FakeHttp()).ensure_schema(ingestor=None)  # type: ignore[arg-type]
    assert sent == build_arcade_schema_statements()


def test_procedure_catalog_mentions_only_algo_procedures() -> None:
    catalog = ArcadeDBDialect().procedure_catalog
    assert "algo." in catalog
    for absent in ("nxalg.", "pagerank.get", "graph_util.", "path.expand"):
        assert absent not in catalog
