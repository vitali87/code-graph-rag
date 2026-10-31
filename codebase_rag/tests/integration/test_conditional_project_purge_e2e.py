"""Integration tests for the conditional project purge (#2479 / PR #3221).

`delete_project(name, expected_root=...)` puts the root check inside the
delete statement. The unit tests pin the routing and params; these run the
real queries against every backend and pin the semantics that matter:

- mismatched expected_root does not delete anything (the repointed-root race
  Greptile's P1 caught);
- a matching expected_root purges the project and its contained nodes, but
  not a Folder another project over the same checkout still claims;
- the residual count reads zero after a fired purge.

Marked integration (Docker-backed Memgraph; Neo4j when configured; the
embedded engine needs neither).
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = pytest.mark.integration

BACKENDS = ("memgraph_ingestor", "neo4j_ingestor", "sqlite_ingestor")


@pytest.fixture
def ingestor(request: pytest.FixtureRequest) -> MemgraphIngestor:
    """Resolve whichever engine's ingestor this parametrisation names."""
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("ingestor", BACKENDS, indirect=True)
class TestConditionalProjectPurge:
    def _seed_project(self, ingestor: MemgraphIngestor, name: str, root: str) -> None:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch("Project", {"name": name, "root_path": root})
        ingestor.ensure_node_batch(
            "Module", {"qualified_name": f"{name}.mod", "name": "mod"}
        )
        ingestor.flush_all()
        # The containment edge the delete traversal walks; the Function rides
        # on the DEFINES edge from the Module, mirroring real ingestion.
        ingestor._execute_query(
            "MATCH (p:Project {name: $name}), (m:Module {qualified_name: $qn}) "
            "CREATE (p)-[:CONTAINS_MODULE]->(m)",
            {"name": name, "qn": f"{name}.mod"},
        )
        ingestor.ensure_node_batch(
            "Function", {"qualified_name": f"{name}.mod.fn", "name": "fn"}
        )
        ingestor.flush_all()
        ingestor._execute_query(
            "MATCH (m:Module {qualified_name: $qn}), "
            "(f:Function {qualified_name: $fn}) CREATE (m)-[:DEFINES]->(f)",
            {"qn": f"{name}.mod", "fn": f"{name}.mod.fn"},
        )
        ingestor.flush_all()

    def test_purge_fires_when_the_root_matches(
        self, ingestor: MemgraphIngestor
    ) -> None:
        self._seed_project(ingestor, "proj", "/repo")
        fired = ingestor.delete_project("proj", expected_root="/repo")
        assert fired is True
        assert "proj" not in ingestor.list_projects()
        residual = ingestor.fetch_all(
            "MATCH (n) WHERE n.name IN ['proj', 'mod', 'fn'] RETURN count(n) AS c"
        )
        assert residual[0]["c"] == 0

    def test_the_residual_count_reads_zero_after_a_fired_purge(
        self, ingestor: MemgraphIngestor
    ) -> None:
        self._seed_project(ingestor, "proj", "/repo")
        params = {cs.KEY_PROJECT_NAME: "proj", cs.KEY_PROJECT_PREFIX: "proj."}

        before = ingestor.fetch_all(cq.CYPHER_COUNT_PROJECT_NODES, params)
        assert before[0][cs.KEY_RESIDUAL_NODES] == 2
        assert ingestor.delete_project("proj", expected_root="/repo") is True
        after = ingestor.fetch_all(cq.CYPHER_COUNT_PROJECT_NODES, params)
        assert after[0][cs.KEY_RESIDUAL_NODES] == 0

    def test_purge_keeps_a_folder_another_project_claims(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # One checkout indexed under two names shares its Folder (#2665):
        # pruning one name must not take the other's tree with it.
        self._seed_project(ingestor, "proj", "/repo")
        self._seed_project(ingestor, "twin", "/repo")
        ingestor._execute_query(
            "MATCH (p:Project) WHERE p.name IN ['proj', 'twin'] "
            "MERGE (f:Folder {absolute_path: '/repo/src'}) "
            "CREATE (p)-[:CONTAINS_FOLDER]->(f)"
        )

        assert ingestor.delete_project("proj", expected_root="/repo") is True
        assert ingestor.list_projects() == ["twin"]
        kept = ingestor.fetch_all(
            "MATCH (f:Folder {absolute_path: '/repo/src'}) RETURN count(f) AS c"
        )
        assert kept[0]["c"] == 1

    def test_purge_does_not_fire_on_a_mismatched_root(
        self, ingestor: MemgraphIngestor
    ) -> None:
        self._seed_project(ingestor, "proj", "/repo")
        fired = ingestor.delete_project("proj", expected_root="/elsewhere")
        assert fired is False
        # The project and everything it owns survives, root_path unchanged.
        assert "proj" in ingestor.list_projects()
        roots = ingestor.list_project_roots()
        assert roots["proj"] == "/repo"
        owned = ingestor.fetch_all(
            "MATCH (m:Module {qualified_name: 'proj.mod'}) RETURN count(m) AS c"
        )
        assert owned[0]["c"] == 1

    def test_purge_does_not_fire_after_a_repoint(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # The concurrent-sync race: the root is repointed between the
        # candidate listing and the delete; the stale expected_root must not
        # purge the project the graph now names at its new location.
        self._seed_project(ingestor, "proj", "/repo")
        ingestor._execute_query(
            "MATCH (p:Project {name: $name}) SET p.root_path = $root",
            {"name": "proj", "root": "/moved"},
        )
        fired = ingestor.delete_project("proj", expected_root="/repo")
        assert fired is False
        assert "proj" in ingestor.list_projects()
        roots = ingestor.list_project_roots()
        assert roots["proj"] == "/moved"

    def test_purge_does_not_fire_for_an_absent_project(
        self, ingestor: MemgraphIngestor
    ) -> None:
        fired = ingestor.delete_project("ghost", expected_root="/nowhere")
        assert fired is False
        assert "ghost" not in ingestor.list_projects()

    def test_unconditional_delete_ignores_the_root(
        self, ingestor: MemgraphIngestor
    ) -> None:
        self._seed_project(ingestor, "proj", "/repo")
        ingestor.delete_project("proj")
        assert "proj" not in ingestor.list_projects()
