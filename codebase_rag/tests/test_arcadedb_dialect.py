from __future__ import annotations

from codebase_rag.constants import (
    NODE_UNIQUE_CONSTRAINTS,
    NodeLabel,
    RelationshipType,
)
from codebase_rag.services.graph.arcadedb import build_arcade_schema_statements


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
