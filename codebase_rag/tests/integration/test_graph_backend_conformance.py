"""The contract every graph backend must satisfy.

Deliberately free of backend conditionals: if an assertion here needs to
branch on the engine, the abstraction leaks and the dialect should absorb
the difference instead.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.services.graph import GraphIngestor

pytestmark = [pytest.mark.integration]

_FN = NodeLabel.FUNCTION.value
_MOD = NodeLabel.MODULE.value
_QN = "qualified_name"


class TestNodeIdentity:
    def test_merge_is_idempotent(self, graph_ingestor: GraphIngestor) -> None:
        for _ in range(2):
            graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f", "name": "f"})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (n:{_FN} {{{_QN}: 'p.m.f'}}) RETURN count(n) AS c"
        )
        assert rows[0]["c"] == 1

    def test_merge_updates_properties_on_second_write(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f", "start_line": 1})
        graph_ingestor.flush_all()
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f", "start_line": 42})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (n:{_FN} {{{_QN}: 'p.m.f'}}) RETURN n.start_line AS line"
        )
        assert len(rows) == 1
        assert rows[0]["line"] == 42

    def test_distinct_keys_make_distinct_nodes(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(f"MATCH (n:{_FN}) RETURN count(n) AS c")
        assert rows[0]["c"] == 2

    def test_node_id_is_an_integer(self, graph_ingestor: GraphIngestor) -> None:
        # vector_store.py keys Qdrant/Milvus payloads on this value, so a
        # string RID would silently break every stored embedding.
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f"})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(f"MATCH (n:{_FN}) RETURN id(n) AS node_id")
        assert isinstance(rows[0]["node_id"], int)


class TestRelationships:
    def test_relationship_properties_round_trip(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_nodes()
        graph_ingestor.ensure_relationship_batch(
            (_FN, _QN, "p.m.a"),
            RelationshipType.CALLS.value,
            (_FN, _QN, "p.m.b"),
            {"line_number": 7},
        )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->(:{_FN}) "
            "RETURN r.line_number AS line"
        )
        assert [r["line"] for r in rows] == [7]

    def test_merge_does_not_duplicate_the_same_edge(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_nodes()
        for _ in range(2):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.a"),
                RelationshipType.CALLS.value,
                (_FN, _QN, "p.m.b"),
            )
            graph_ingestor.flush_relationships()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->(:{_FN}) "
            "RETURN count(r) AS c"
        )
        assert rows[0]["c"] == 1

    def test_flows_to_parallel_edges_survive_merge(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Regression guard for issue #722. MERGE_KEY_PROPS_BY_REL puts
        # (via, kind) into the MERGE pattern so two provenance edges between
        # the same pair stay distinct instead of collapsing into one.
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.src"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.dst"})
        graph_ingestor.flush_nodes()
        for via, kind in (("arg", "direct"), ("ret", "direct")):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.src"),
                RelationshipType.FLOWS_TO.value,
                (_FN, _QN, "p.m.dst"),
                {"via": via, "kind": kind},
            )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.FLOWS_TO.value}]->(:{_FN}) "
            "RETURN r.via AS via ORDER BY via"
        )
        assert [r["via"] for r in rows] == ["arg", "ret"]


class TestConcurrency:
    def test_parallel_flush_into_one_hot_target(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Many CALLS edges converging on one vertex is the exact shape that
        # provokes optimistic-write conflicts on MVCC engines. This is the
        # test that exercises the retry path.
        target = "p.m.hot"
        callers = [f"p.m.c{i}" for i in range(60)]
        graph_ingestor.ensure_node_batch(_FN, {_QN: target})
        for qn in callers:
            graph_ingestor.ensure_node_batch(_FN, {_QN: qn})
        graph_ingestor.flush_nodes()

        def write(qn: str) -> None:
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, qn), RelationshipType.CALLS.value, (_FN, _QN, target)
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, callers))
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->"
            f"(:{_FN} {{{_QN}: '{target}'}}) RETURN count(r) AS c"
        )
        assert rows[0]["c"] == len(callers)


class TestAdminOperations:
    def _seed_project(self, ingestor: GraphIngestor, name: str) -> None:
        ingestor.ensure_node_batch(
            NodeLabel.PROJECT.value, {"name": name, "root_path": f"/tmp/{name}"}
        )
        ingestor.ensure_node_batch(_MOD, {_QN: f"{name}.mod"})
        ingestor.ensure_node_batch(_FN, {_QN: f"{name}.mod.fn"})
        ingestor.flush_nodes()
        ingestor.ensure_relationship_batch(
            (NodeLabel.PROJECT.value, "name", name),
            RelationshipType.CONTAINS_MODULE.value,
            (_MOD, _QN, f"{name}.mod"),
        )
        ingestor.ensure_relationship_batch(
            (_MOD, _QN, f"{name}.mod"),
            RelationshipType.DEFINES.value,
            (_FN, _QN, f"{name}.mod.fn"),
        )
        ingestor.flush_all()

    def test_list_projects(self, graph_ingestor: GraphIngestor) -> None:
        self._seed_project(graph_ingestor, "alpha")
        self._seed_project(graph_ingestor, "beta")
        assert graph_ingestor.list_projects() == ["alpha", "beta"]

    def test_list_project_roots(self, graph_ingestor: GraphIngestor) -> None:
        self._seed_project(graph_ingestor, "alpha")
        assert graph_ingestor.list_project_roots() == {"alpha": "/tmp/alpha"}

    def test_delete_project_removes_its_subtree(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        self._seed_project(graph_ingestor, "alpha")
        self._seed_project(graph_ingestor, "beta")
        graph_ingestor.delete_project("alpha")

        rows = graph_ingestor.fetch_all(
            f"MATCH (n) WHERE n.{_QN} STARTS WITH 'alpha.' RETURN count(n) AS c"
        )
        assert rows[0]["c"] == 0

    def test_delete_project_leaves_siblings_intact(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        self._seed_project(graph_ingestor, "alpha")
        self._seed_project(graph_ingestor, "beta")
        graph_ingestor.delete_project("alpha")

        assert graph_ingestor.list_projects() == ["beta"]
        rows = graph_ingestor.fetch_all(
            f"MATCH (n) WHERE n.{_QN} STARTS WITH 'beta.' RETURN count(n) AS c"
        )
        assert rows[0]["c"] == 2

    def test_clean_database_empties_the_graph(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        self._seed_project(graph_ingestor, "alpha")
        graph_ingestor.clean_database()

        rows = graph_ingestor.fetch_all("MATCH (n) RETURN count(n) AS c")
        assert rows[0]["c"] == 0

    def test_export_graph_to_dict(self, graph_ingestor: GraphIngestor) -> None:
        self._seed_project(graph_ingestor, "alpha")
        data = graph_ingestor.export_graph_to_dict()

        assert data["metadata"]["total_nodes"] == 3
        assert data["metadata"]["total_relationships"] == 2
        assert all(isinstance(n["node_id"], int) for n in data["nodes"])


class TestSchemaBootstrap:
    def test_ensure_constraints_is_idempotent(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_constraints()
        graph_ingestor.ensure_constraints()

        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f"})
        graph_ingestor.flush_all()
        rows = graph_ingestor.fetch_all(f"MATCH (n:{_FN}) RETURN count(n) AS c")
        assert rows[0]["c"] == 1


class TestResourcePruning:
    def test_unanchored_resources_are_pruned_on_delete_project(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Resource nodes carry no project prefix, so delete_project only
        # strips their edges; the prune pass must then remove the ones this
        # project alone anchored.
        graph_ingestor.ensure_node_batch(
            NodeLabel.PROJECT.value, {"name": "alpha", "root_path": "/tmp/alpha"}
        )
        graph_ingestor.ensure_node_batch(_MOD, {_QN: "alpha.mod"})
        graph_ingestor.ensure_node_batch(
            NodeLabel.RESOURCE.value, {_QN: "res1", "kind": "ENDPOINT"}
        )
        graph_ingestor.flush_nodes()
        graph_ingestor.ensure_relationship_batch(
            (NodeLabel.PROJECT.value, "name", "alpha"),
            RelationshipType.CONTAINS_MODULE.value,
            (_MOD, _QN, "alpha.mod"),
        )
        graph_ingestor.ensure_relationship_batch(
            (_MOD, _QN, "alpha.mod"),
            RelationshipType.EXPOSES.value,
            (NodeLabel.RESOURCE.value, _QN, "res1"),
        )
        graph_ingestor.flush_all()

        graph_ingestor.delete_project("alpha")

        rows = graph_ingestor.fetch_all(
            f"MATCH (r:{NodeLabel.RESOURCE.value}) RETURN count(r) AS c"
        )
        assert rows[0]["c"] == 0
