"""Liveness rules for shared Resource nodes (dogfood findings on #425).

``delete-project`` and incremental rebuilds strip edges off prefix-less
Resource nodes without deleting them; cleanup must remove exactly the
resources whose flow component no longer reaches a code node, in one
atomic statement so a concurrent writer cannot anchor a resource between
a snapshot read and the delete. Run on every engine, since each runs the
same statement: Memgraph, Neo4j and the embedded one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag.services.resource_cleanup import prune_unanchored_resources

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

BACKENDS = ("memgraph_ingestor", "neo4j_ingestor", "sqlite_ingestor")


@pytest.fixture
def ingestor(request: pytest.FixtureRequest) -> MemgraphIngestor:
    return request.getfixturevalue(request.param)


def _seed(ingestor: MemgraphIngestor, cypher: str) -> None:
    ingestor.execute_write(cypher)


def _remaining(ingestor: MemgraphIngestor) -> list[str]:
    rows = ingestor.fetch_all(
        "MATCH (r:Resource) RETURN r.qualified_name AS qn ORDER BY qn"
    )
    return [str(r["qn"]) for r in rows]


@pytest.mark.parametrize("ingestor", BACKENDS, indirect=True)
class TestPruneUnanchoredResources:
    def test_anchored_resource_survives(self, ingestor: MemgraphIngestor) -> None:
        _seed(
            ingestor,
            "CREATE (f:Function {qualified_name: 'p.main'}), "
            "(r:Resource {qualified_name: 'env', kind: 'ENV'}) "
            "CREATE (f)-[:READS_FROM]->(r)",
        )

        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == ["env"]

    def test_edgeless_resource_deleted(self, ingestor: MemgraphIngestor) -> None:
        _seed(
            ingestor,
            "CREATE (:Resource {qualified_name: 'stale', kind: 'ENDPOINT'})",
        )

        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == []

    def test_flow_source_anchored_through_sink_survives(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # Regression: `fmt.Println(os.Getenv(...))` emits only the derived
        # ENV -FLOWS_TO-> STDOUT edge for the source, so the ENV resource
        # is live purely through its anchored sink.
        _seed(
            ingestor,
            "CREATE (f:Function {qualified_name: 'p.main'}), "
            "(e:Resource {qualified_name: 'env', kind: 'ENV'}), "
            "(s:Resource {qualified_name: 'stdout', kind: 'STDOUT'}) "
            "CREATE (f)-[:WRITES_TO]->(s), (e)-[:FLOWS_TO]->(s)",
        )

        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == ["env", "stdout"]

    def test_floating_flow_pair_deleted(self, ingestor: MemgraphIngestor) -> None:
        # A flow edge between two resources must not keep the pair alive
        # once the code that anchored either side is gone.
        _seed(
            ingestor,
            "CREATE (e:Resource {qualified_name: 'env', kind: 'ENV'}), "
            "(s:Resource {qualified_name: 'stdout', kind: 'STDOUT'}) "
            "CREATE (e)-[:FLOWS_TO]->(s)",
        )

        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == []

    def test_resolves_to_does_not_propagate_liveness(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # RESOLVES_TO is derived on every sync: it must neither anchor a
        # resource nor carry liveness from a live URL to a dead endpoint.
        _seed(
            ingestor,
            "CREATE (f:Function {qualified_name: 'p.caller'}), "
            "(u:Resource {qualified_name: 'url', kind: 'NETWORK'}), "
            "(d:Resource {qualified_name: 'dead_ep', kind: 'ENDPOINT'}) "
            "CREATE (f)-[:READS_FROM]->(u), (u)-[:RESOLVES_TO]->(d)",
        )

        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == ["url"]

    def test_empty_graph_is_noop(self, ingestor: MemgraphIngestor) -> None:
        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == []

    def test_liveness_travels_eight_flows_either_way(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # The walk's cap: r8 is eight flows from the read r0, r9 nine.
        nodes = ", ".join(
            f"(r{i}:Resource {{qualified_name: 'r{i}'}})" for i in range(10)
        )
        flows = ", ".join(
            f"(r{i})-[:FLOWS_TO]->(r{i + 1})"
            if i % 2
            else f"(r{i + 1})-[:FLOWS_TO]->(r{i})"
            for i in range(9)
        )
        _seed(
            ingestor,
            f"CREATE (f:Function {{qualified_name: 'p.main'}}), {nodes} "
            f"CREATE (f)-[:READS_FROM]->(r0), {flows}",
        )

        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == [f"r{i}" for i in range(9)]

    def test_a_flow_mesh_keeps_only_its_anchored_side(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # Regression (#3820): sources flowing into shared sinks give a path
        # count that grows as a power of the mesh, which a per-path walk
        # had to enumerate.
        for side in ("live", "dead"):
            _seed(
                ingestor,
                "UNWIND range(1, 12) AS i "
                f"CREATE (:Resource {{qualified_name: '{side}.source' + toString(i)}})",
            )
            _seed(
                ingestor,
                "UNWIND range(1, 3) AS i "
                f"CREATE (:Resource {{qualified_name: '{side}.sink' + toString(i)}})",
            )
            _seed(
                ingestor,
                "MATCH (a:Resource), (b:Resource) "
                f"WHERE a.qualified_name STARTS WITH '{side}.source' "
                f"AND b.qualified_name STARTS WITH '{side}.sink' "
                f"CREATE (a)-[:FLOWS_TO]->(b)",
            )
        _seed(
            ingestor,
            "MATCH (s:Resource {qualified_name: 'live.sink2'}) "
            "CREATE (:Function {qualified_name: 'p.main'})-[:WRITES_TO]->(s)",
        )

        prune_unanchored_resources(ingestor)

        remaining = _remaining(ingestor)
        assert len(remaining) == 15
        assert all(qn.startswith("live.") for qn in remaining)

    def test_flows_of_a_resource_into_itself_carry_no_liveness(
        self, ingestor: MemgraphIngestor
    ) -> None:
        # golang/tools' shape (#3820): the dynamic FILE resource flows into
        # itself from eleven call sites, and every order of the eleven was a
        # path of the per-path walk.
        for name in ("live", "dead"):
            _seed(
                ingestor,
                f"CREATE (r:Resource {{qualified_name: '{name}'}}) "
                "WITH r UNWIND range(1, 11) AS i CREATE (r)-[:FLOWS_TO]->(r)",
            )
        _seed(
            ingestor,
            "MATCH (r:Resource {qualified_name: 'live'}) "
            "CREATE (:Function {qualified_name: 'p.main'})-[:WRITES_TO]->(r)",
        )

        prune_unanchored_resources(ingestor)

        assert _remaining(ingestor) == ["live"]
