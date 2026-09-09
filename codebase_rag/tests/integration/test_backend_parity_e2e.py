"""The same graph, built and read back on each engine (issue #1590).

Unit tests pin the statements the dialects emit; only a real server shows
that those statements are ACCEPTED and mean the same thing. Each test
takes the ingestor fixture by name and runs identical assertions, so a
divergence shows up as one engine failing a test the other passes.

The Neo4j tests skip without Docker or the optional `neo4j` package;
`neo4j_container` handles both.
"""

from __future__ import annotations

import pytest

from codebase_rag.constants import NODE_UNIQUE_CONSTRAINTS
from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = pytest.mark.integration

BACKENDS = ("memgraph_ingestor", "neo4j_ingestor")


@pytest.fixture
def ingestor(request: pytest.FixtureRequest) -> MemgraphIngestor:
    """Resolve whichever engine's ingestor this parametrisation names."""
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("ingestor", BACKENDS, indirect=True)
class TestSchemaSetup:
    def test_the_constraints_actually_exist_afterwards(
        self, ingestor: MemgraphIngestor
    ) -> None:
        """The DDL must be ACCEPTED, not merely issued.

        `ensure_constraints` wraps every statement in `except Exception:
        pass`, so wrong-dialect DDL fails silently and the graph is built
        with nothing enforced. Counting nodes cannot detect that -- MERGE
        deduplicates on its own whether or not a constraint exists, which
        is why an earlier version of this test passed against a
        deliberately broken dialect. Read the server's own constraint
        catalogue back instead.
        """
        ingestor.ensure_constraints()
        # `_execute_query`, not `fetch_all`: the latter appends Memgraph's
        # QUERY MEMORY LIMIT, which SHOW CONSTRAINT INFO rejects. This is
        # the same path `_migrate_legacy_path_keys` uses in production.
        rows = ingestor._execute_query(ingestor._dialect.show_constraints())
        assert rows, "no constraints were created"
        matched = [
            (label, prop)
            for label, prop in NODE_UNIQUE_CONSTRAINTS.items()
            if any(
                ingestor._dialect.constraint_row_matches(row, label, prop)
                for row in rows
            )
        ]
        assert len(matched) == len(NODE_UNIQUE_CONSTRAINTS)

    def test_a_duplicate_unique_key_is_rejected(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # The parallel MERGE flush relies on the constraint for
        # correctness, so prove the server enforces it rather than
        # trusting that the DDL was accepted.
        ingestor.ensure_constraints()
        ingestor.execute_write("CREATE (n:Project {name: 'dup', path: '/a'})")
        with pytest.raises(Exception, match="(?i)constraint|already exists"):
            ingestor.execute_write("CREATE (n:Project {name: 'dup', path: '/b'})")

    def test_running_it_twice_is_idempotent(self, ingestor: MemgraphIngestor) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_constraints()


@pytest.mark.parametrize("ingestor", BACKENDS, indirect=True)
class TestNodesAndRelationships:
    def test_a_node_round_trips_with_its_properties(
        self, ingestor: MemgraphIngestor
    ) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch(
            "Module", {"qualified_name": "a.b", "name": "b", "path": "a/b.py"}
        )
        ingestor.flush_all()
        rows = ingestor.fetch_all(
            "MATCH (m:Module {qualified_name: 'a.b'}) "
            "RETURN m.name AS name, m.path AS path"
        )
        assert rows == [{"name": "b", "path": "a/b.py"}]

    def test_merge_does_not_duplicate_on_re_ingest(
        self, ingestor: MemgraphIngestor
    ) -> None:
        ingestor.ensure_constraints()
        for _ in range(3):
            ingestor.ensure_node_batch("Module", {"qualified_name": "a.b", "name": "b"})
        ingestor.flush_all()
        rows = ingestor.fetch_all("MATCH (m:Module) RETURN count(m) AS c")
        assert rows[0]["c"] == 1

    def test_a_relationship_links_two_nodes(self, ingestor: MemgraphIngestor) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch("Module", {"qualified_name": "a", "name": "a"})
        ingestor.ensure_node_batch("Module", {"qualified_name": "b", "name": "b"})
        ingestor.flush_all()
        ingestor.ensure_relationship_batch(
            ("Module", "qualified_name", "a"),
            "IMPORTS",
            ("Module", "qualified_name", "b"),
        )
        ingestor.flush_all()
        rows = ingestor.fetch_all(
            "MATCH (:Module {qualified_name: 'a'})-[r:IMPORTS]->"
            "(:Module {qualified_name: 'b'}) RETURN count(r) AS c"
        )
        assert rows[0]["c"] == 1

    def test_relationship_properties_survive(self, ingestor: MemgraphIngestor) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch("Module", {"qualified_name": "a", "name": "a"})
        ingestor.ensure_node_batch("Module", {"qualified_name": "b", "name": "b"})
        ingestor.flush_all()
        ingestor.ensure_relationship_batch(
            ("Module", "qualified_name", "a"),
            "IMPORTS",
            ("Module", "qualified_name", "b"),
            {"line": 12},
        )
        ingestor.flush_all()
        rows = ingestor.fetch_all("MATCH ()-[r:IMPORTS]->() RETURN r.line AS line")
        assert rows == [{"line": 12}]

    def test_a_batch_larger_than_batch_size_is_written_whole(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # Exercises the UNWIND $batch path and the mid-run auto-flush.
        ingestor.ensure_constraints()
        for i in range(250):
            ingestor.ensure_node_batch(
                "Module", {"qualified_name": f"m{i}", "name": f"m{i}"}
            )
        ingestor.flush_all()
        rows = ingestor.fetch_all("MATCH (m:Module) RETURN count(m) AS c")
        assert rows[0]["c"] == 250


@pytest.mark.parametrize("ingestor", BACKENDS, indirect=True)
class TestReadPath:
    def test_fetch_all_returns_named_columns(self, ingestor: MemgraphIngestor) -> None:
        # Column names come from `cursor.description`, which the Neo4j
        # adapter has to synthesise; a regression there yields positional
        # tuples or empty dicts rather than an error.
        rows = ingestor.fetch_all("RETURN 1 AS one, 2 AS two")
        assert rows == [{"one": 1, "two": 2}]

    def test_a_query_with_no_rows_returns_an_empty_list(
        self, ingestor: MemgraphIngestor
    ) -> None:
        assert ingestor.fetch_all("MATCH (n:NothingHere) RETURN n") == []

    def test_parameters_are_bound(self, ingestor: MemgraphIngestor) -> None:
        rows = ingestor.fetch_all("RETURN $value AS v", {"value": 7})
        assert rows == [{"v": 7}]

    def test_execute_write_persists(self, ingestor: MemgraphIngestor) -> None:
        ingestor.execute_write("CREATE (n:Marker {id: $id})", {"id": "x"})
        rows = ingestor.fetch_all("MATCH (n:Marker) RETURN n.id AS id")
        assert rows == [{"id": "x"}]


@pytest.mark.parametrize("ingestor", BACKENDS, indirect=True)
class TestProjectLifecycle:
    def test_a_project_is_listed_then_deleted(self, ingestor: MemgraphIngestor) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch("Project", {"name": "proj", "path": "/p"})
        ingestor.flush_all()
        assert "proj" in ingestor.list_projects()
        ingestor.delete_project("proj")
        assert "proj" not in ingestor.list_projects()

    def test_clean_database_empties_the_graph(self, ingestor: MemgraphIngestor) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch("Module", {"qualified_name": "a", "name": "a"})
        ingestor.flush_all()
        ingestor.clean_database()
        assert ingestor.fetch_all("MATCH (n) RETURN count(n) AS c")[0]["c"] == 0

    def test_export_reports_what_was_written(self, ingestor: MemgraphIngestor) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch("Module", {"qualified_name": "a", "name": "a"})
        ingestor.ensure_node_batch("Module", {"qualified_name": "b", "name": "b"})
        ingestor.flush_all()
        exported = ingestor.export_graph_to_dict()
        assert exported["metadata"]["total_nodes"] == 2
