"""Per-engine Cypher differences (issue #1590).

Two properties matter more than the literal strings: the Memgraph output
must be exactly what the project emitted before the dialect seam existed
(or every current install regresses), and no engine's syntax may leak
into another's statements.
"""

from __future__ import annotations

import pytest

from codebase_rag.constants import (
    KEY_NAME,
    LEGACY_NODE_CONSTRAINTS,
    NODE_NAME_INDEXES,
    NODE_UNIQUE_CONSTRAINTS,
)
from codebase_rag.graph_dialects import (
    DIALECT_MEMGRAPH,
    DIALECT_NEO4J,
    GraphDialect,
    MemgraphDialect,
    Neo4jDialect,
    available_dialects,
    get_dialect,
)

# Every (label, prop) pair the ingestor actually builds DDL for.
REAL_PAIRS = (
    list(NODE_UNIQUE_CONSTRAINTS.items())
    + [(label, KEY_NAME) for label in NODE_NAME_INDEXES]
    + list(LEGACY_NODE_CONSTRAINTS)
)


class TestRegistry:
    def test_both_engines_are_registered(self) -> None:
        assert available_dialects() == (DIALECT_MEMGRAPH, DIALECT_NEO4J)

    def test_lookup_is_case_and_whitespace_insensitive(self) -> None:
        assert get_dialect("  NEO4J ").name == DIALECT_NEO4J

    def test_an_unknown_engine_is_rejected(self) -> None:
        # Falling back to a default would send Memgraph DDL to another
        # server, and the ingestor swallows DDL errors, so the mistake
        # would only surface as a silently unconstrained graph.
        with pytest.raises(ValueError, match="Unknown graph backend"):
            get_dialect("postgres")

    def test_the_error_names_the_valid_engines(self) -> None:
        with pytest.raises(ValueError, match="memgraph, neo4j"):
            get_dialect("postgres")

    @pytest.mark.parametrize("name", available_dialects())
    def test_every_registered_engine_satisfies_the_protocol(self, name: str) -> None:
        assert isinstance(get_dialect(name), GraphDialect)


class TestMemgraphIsUnchanged:
    """The exact statements this project emitted before the seam existed."""

    @pytest.mark.parametrize(("label", "prop"), REAL_PAIRS)
    def test_create_constraint_text(self, label: str, prop: str) -> None:
        assert (
            MemgraphDialect().create_constraint(label, prop)
            == f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"
        )

    @pytest.mark.parametrize(("label", "prop"), REAL_PAIRS)
    def test_drop_constraint_text(self, label: str, prop: str) -> None:
        assert (
            MemgraphDialect().drop_constraint(label, prop)
            == f"DROP CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"
        )

    @pytest.mark.parametrize(("label", "prop"), REAL_PAIRS)
    def test_create_index_text(self, label: str, prop: str) -> None:
        assert (
            MemgraphDialect().create_index(label, prop)
            == f"CREATE INDEX ON :{label}({prop});"
        )

    def test_show_constraints_text(self) -> None:
        assert MemgraphDialect().show_constraints() == "SHOW CONSTRAINT INFO;"

    def test_memory_limit_is_appended_before_the_semicolon(self) -> None:
        assert (
            MemgraphDialect().apply_memory_limit("MATCH (n) RETURN n;", 4096)
            == "MATCH (n) RETURN n QUERY MEMORY LIMIT 4096 MB;"
        )

    def test_memory_limit_is_not_applied_twice(self) -> None:
        once = MemgraphDialect().apply_memory_limit("MATCH (n) RETURN n;", 4096)
        assert MemgraphDialect().apply_memory_limit(once, 4096) == once

    def test_a_row_is_matched_by_label_and_properties(self) -> None:
        row = {"label": "Folder", "properties": ["path"]}
        assert MemgraphDialect().constraint_row_matches(row, "Folder", "path")

    def test_a_different_label_does_not_match(self) -> None:
        row = {"label": "File", "properties": ["path"]}
        assert not MemgraphDialect().constraint_row_matches(row, "Folder", "path")


class TestNeo4j:
    def test_create_constraint_uses_require_and_is_idempotent(self) -> None:
        assert Neo4jDialect().create_constraint("Folder", "path") == (
            "CREATE CONSTRAINT cgr_folder_path IF NOT EXISTS "
            "FOR (n:Folder) REQUIRE n.path IS UNIQUE"
        )

    def test_drop_constraint_addresses_by_name(self) -> None:
        # Neo4j 5 has no pattern-based DROP CONSTRAINT at all, so the name
        # has to be derivable from (label, prop) without a database read.
        assert (
            Neo4jDialect().drop_constraint("Folder", "path")
            == "DROP CONSTRAINT cgr_folder_path IF EXISTS"
        )

    def test_create_index_uses_the_for_on_form(self) -> None:
        assert Neo4jDialect().create_index("Class", "name") == (
            "CREATE INDEX cgr_class_name IF NOT EXISTS FOR (n:Class) ON (n.name)"
        )

    def test_show_constraints_has_no_info_suffix(self) -> None:
        assert Neo4jDialect().show_constraints() == "SHOW CONSTRAINTS"

    def test_the_memory_limit_is_dropped_entirely(self) -> None:
        # Neo4j has no per-query memory clause; appending Memgraph's would
        # make every single read in the system a syntax error.
        query = "MATCH (n) RETURN n;"
        assert Neo4jDialect().apply_memory_limit(query, 4096) == query

    def test_a_row_matches_through_the_labelsortypes_list(self) -> None:
        row = {"labelsOrTypes": ["Folder"], "properties": ["path"], "name": "x"}
        assert Neo4jDialect().constraint_row_matches(row, "Folder", "path")

    def test_a_memgraph_shaped_row_does_not_match(self) -> None:
        # The regression this guards: comparing Neo4j's list column to a
        # bare label matches nothing, which would make the legacy-key
        # migration silently no-op instead of failing loudly.
        row = {"label": "Folder", "properties": ["path"]}
        assert not Neo4jDialect().constraint_row_matches(row, "Folder", "path")

    def test_a_different_property_does_not_match(self) -> None:
        row = {"labelsOrTypes": ["Folder"], "properties": ["name"]}
        assert not Neo4jDialect().constraint_row_matches(row, "Folder", "path")


class TestNoCrossDialectLeakage:
    @pytest.mark.parametrize(("label", "prop"), REAL_PAIRS)
    def test_neo4j_never_emits_memgraph_syntax(self, label: str, prop: str) -> None:
        d = Neo4jDialect()
        statements = (
            d.create_constraint(label, prop),
            d.drop_constraint(label, prop),
            d.create_index(label, prop),
            d.show_constraints(),
        )
        for statement in statements:
            assert "ASSERT" not in statement
            assert "CONSTRAINT INFO" not in statement
            assert "QUERY MEMORY LIMIT" not in statement

    @pytest.mark.parametrize(("label", "prop"), REAL_PAIRS)
    def test_memgraph_never_emits_neo4j_syntax(self, label: str, prop: str) -> None:
        d = MemgraphDialect()
        statements = (
            d.create_constraint(label, prop),
            d.drop_constraint(label, prop),
            d.create_index(label, prop),
        )
        for statement in statements:
            assert "REQUIRE" not in statement
            assert "IF NOT EXISTS" not in statement

    def test_constraint_names_are_unique_per_pair(self) -> None:
        # A collision would make one DROP remove another label's constraint.
        d = Neo4jDialect()
        names = {d.create_constraint(label, prop) for label, prop in REAL_PAIRS}
        assert len(names) == len(set(REAL_PAIRS))


class TestLegacyConstraintIsDroppedByItsRealName:
    """A legacy constraint predates this code and may be named anything.

    Neo4j drops constraints by name only, so detecting one by label and
    property but dropping a name we derived leaves the obsolete key
    enforced -- silently, since `ensure_constraints` swallows DDL errors.
    """

    def test_neo4j_reads_the_servers_own_name(self) -> None:
        row = {
            "name": "legacy_folder_path_uc",
            "labelsOrTypes": ["Folder"],
            "properties": ["path"],
        }
        assert Neo4jDialect().constraint_row_name(row) == "legacy_folder_path_uc"

    def test_neo4j_drops_the_discovered_name(self) -> None:
        assert (
            Neo4jDialect().drop_constraint("Folder", "path", "legacy_folder_path_uc")
            == "DROP CONSTRAINT legacy_folder_path_uc IF EXISTS"
        )

    def test_neo4j_falls_back_to_the_derived_name(self) -> None:
        assert (
            Neo4jDialect().drop_constraint("Folder", "path", None)
            == "DROP CONSTRAINT cgr_folder_path IF EXISTS"
        )

    def test_a_row_without_a_name_yields_none(self) -> None:
        row = {"labelsOrTypes": ["Folder"], "properties": ["path"]}
        assert Neo4jDialect().constraint_row_name(row) is None

    def test_an_empty_name_yields_none(self) -> None:
        # An empty string would produce `DROP CONSTRAINT  IF EXISTS`.
        row = {"name": "", "labelsOrTypes": ["Folder"], "properties": ["path"]}
        assert Neo4jDialect().constraint_row_name(row) is None

    def test_memgraph_ignores_the_name_entirely(self) -> None:
        # Memgraph addresses constraints by pattern, so passing a name
        # must not change the statement it emits.
        dialect = MemgraphDialect()
        assert dialect.drop_constraint("Folder", "path", "anything") == (
            dialect.drop_constraint("Folder", "path", None)
        )

    def test_memgraph_reports_no_name(self) -> None:
        assert MemgraphDialect().constraint_row_name({"label": "Folder"}) is None
