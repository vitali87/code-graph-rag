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
_METHOD = NodeLabel.METHOD.value
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

    def test_merge_does_not_duplicate_the_same_edge_within_one_batch(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Real ArcadeDB divergence found while building this suite: unlike
        # nodes (deduped via a unique index), relationships have no index on
        # ArcadeDB, so its Cypher MERGE only sees already-committed state --
        # not an earlier row's write from the *same* UNWIND-batched
        # statement. Two identical rows flushed in one batch (no
        # flush_relationships() between them, unlike the test above) used to
        # create two edges on ArcadeDB while Memgraph's engine converged on
        # one. Fixed by pre-merging same-pattern rows client-side in
        # ArcadeDBIngestor._flush_rel_pattern_group before the batch is
        # sent; this guards the fix.
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_nodes()
        for _ in range(2):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.a"),
                RelationshipType.CALLS.value,
                (_FN, _QN, "p.m.b"),
            )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->(:{_FN}) "
            "RETURN count(r) AS c"
        )
        assert rows[0]["c"] == 1

    def test_duplicate_edge_props_overlay_last_value_wins_within_one_batch(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Regression guard for the overlay ORDER of ArcadeDB's client-side
        # same-batch dedup (_dedupe_rows_sharing_a_merge_pattern). Two rows
        # merging onto the same edge in one batch, differing only in
        # `line_number`, must converge on the SECOND (later) value -- this
        # is the assertion that would catch a dedup that kept the FIRST row
        # instead of overlaying in arrival order, which is the most likely
        # silent divergence class for this fix (Memgraph's own
        # SET-after-MERGE naturally applies later writes last; the
        # client-side dedup must reproduce that, not invert it).
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_nodes()
        for line_number in (1, 2):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.a"),
                RelationshipType.CALLS.value,
                (_FN, _QN, "p.m.b"),
                {"line_number": line_number},
            )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->(:{_FN}) "
            "RETURN r.line_number AS line"
        )
        assert [r["line"] for r in rows] == [2]

    def test_flows_to_parallel_edges_survive_merge(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Regression guard for issue #722. MERGE_KEY_PROPS_BY_REL groups
        # rows by which of (via, kind) are PRESENT, not by their values, so
        # the two rows below deliberately carry different prop shapes (one
        # has both via and kind, the other has via only) to exercise that
        # signature-splitting logic. Two rows that both carried the same two
        # keys (as an earlier version of this test did) would still pass
        # even if per-signature splitting were deleted outright, since a
        # naive (via, kind) MERGE key alone already disambiguates them --
        # see the asymmetric case at test_cypher_queries.py:778-807.
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.src"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.dst"})
        graph_ingestor.flush_nodes()
        for props in ({"via": "arg", "kind": "direct"}, {"via": "ret"}):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.src"),
                RelationshipType.FLOWS_TO.value,
                (_FN, _QN, "p.m.dst"),
                props,
            )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.FLOWS_TO.value}]->(:{_FN}) "
            "RETURN r.via AS via, r.kind AS kind ORDER BY r.via"
        )
        assert [(r["via"], r["kind"]) for r in rows] == [
            ("arg", "direct"),
            ("ret", None),
        ]

    def test_flows_to_same_signature_edges_survive_merge_within_one_batch(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Regression guard for the client-side pre-merge dedup added to fix
        # ArcadeDB's same-batch relationship MERGE gap (see
        # test_merge_does_not_duplicate_the_same_edge_within_one_batch
        # above). Both rows here carry via+kind, so both share ONE
        # merge-key signature and land in the SAME by_keys group -- unlike
        # test_flows_to_parallel_edges_survive_merge's asymmetric rows,
        # which get split into different groups before dedup ever runs on
        # them. A dedup key that only looked at (from_val, to_val) would
        # silently collapse these two into one edge and pass every other
        # test in this file; only a dedup key that also includes each row's
        # actual via/kind VALUES keeps both. Flushed in one batch (single
        # flush_all, no flush_relationships between the two adds) so the
        # dedup path in _flush_rel_pattern_group actually runs.
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.src2"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.dst2"})
        graph_ingestor.flush_nodes()
        for via in ("kw:username", "kw:password"):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.src2"),
                RelationshipType.FLOWS_TO.value,
                (_FN, _QN, "p.m.dst2"),
                {"via": via, "kind": "arg"},
            )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.FLOWS_TO.value}]->(:{_FN}) "
            "RETURN r.via AS via ORDER BY via"
        )
        assert [r["via"] for r in rows] == ["kw:password", "kw:username"]


class TestConcurrency:
    def test_parallel_flush_into_one_hot_target(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Many CALLS edges converging on one vertex is the exact shape that
        # provokes optimistic-write conflicts on MVCC engines. Splitting the
        # callers across two node kinds (Function, Method) gives
        # ensure_relationship_batch two distinct (from_label, rel_type,
        # to_label) patterns instead of one, so the flush fans out into two
        # groups that write on separate connections concurrently -- real
        # concurrent writes at the database layer, not just concurrent
        # buffer appends -- while both groups still converge on the same
        # hot vertex. This mirrors real ingest, where a hot function is
        # called from several node kinds, and is the test that exercises
        # the retry path.
        target = "p.m.hot"
        fn_callers = [f"p.m.c{i}" for i in range(30)]
        method_callers = [f"p.m.C.m{i}" for i in range(30)]
        graph_ingestor.ensure_node_batch(_FN, {_QN: target})
        for qn in fn_callers:
            graph_ingestor.ensure_node_batch(_FN, {_QN: qn})
        for qn in method_callers:
            graph_ingestor.ensure_node_batch(_METHOD, {_QN: qn})
        graph_ingestor.flush_nodes()

        callers: list[tuple[str, str]] = [(_FN, qn) for qn in fn_callers] + [
            (_METHOD, qn) for qn in method_callers
        ]

        def write(spec: tuple[str, str]) -> None:
            label, qn = spec
            graph_ingestor.ensure_relationship_batch(
                (label, _QN, qn), RelationshipType.CALLS.value, (_FN, _QN, target)
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, callers))
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH ()-[r:{RelationshipType.CALLS.value}]->"
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
        # Re-running ensure_constraints() must stay safe -- this is the
        # idempotency being tested, not merely a setup step.
        graph_ingestor.ensure_constraints()
        graph_ingestor.ensure_constraints()

        # Exercise the constraint itself, not the ingestor's own MERGE-based
        # dedup (already covered by test_merge_is_idempotent): a raw CREATE
        # bypasses ensure_node_batch's batching entirely, so a second CREATE
        # on the same unique key only survives if no constraint enforces it.
        # The offending write may raise or be silently rejected depending on
        # the engine, so either outcome is accepted here -- what must hold
        # is the end state: exactly one node with this key.
        create_query = f"CREATE (n:{_FN} {{{_QN}: 'p.m.constrained'}})"
        graph_ingestor.execute_write(create_query)
        try:
            graph_ingestor.execute_write(create_query)
        except Exception:
            pass

        rows = graph_ingestor.fetch_all(
            f"MATCH (n:{_FN} {{{_QN}: 'p.m.constrained'}}) RETURN count(n) AS c"
        )
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
