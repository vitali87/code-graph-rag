"""Liveness rules for shared Resource nodes (dogfood findings on #425).

``delete-project`` and incremental rebuilds strip edges off prefix-less
Resource nodes without deleting them; cleanup must remove exactly the
resources whose flow component no longer reaches a code node, in one
atomic statement so a concurrent writer cannot anchor a resource between
a snapshot read and the delete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag.services.resource_cleanup import prune_unanchored_resources

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def _seed(ingestor: MemgraphIngestor, cypher: str) -> None:
    ingestor.execute_write(cypher)


def _remaining(ingestor: MemgraphIngestor) -> list[str]:
    rows = ingestor.fetch_all(
        "MATCH (r:Resource) RETURN r.qualified_name AS qn ORDER BY qn"
    )
    return [str(r["qn"]) for r in rows]


class TestPruneUnanchoredResources:
    def test_anchored_resource_survives(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        _seed(
            memgraph_ingestor,
            "CREATE (f:Function {qualified_name: 'p.main'}), "
            "(r:Resource {qualified_name: 'env', kind: 'ENV'}) "
            "CREATE (f)-[:READS_FROM]->(r)",
        )

        prune_unanchored_resources(memgraph_ingestor)

        assert _remaining(memgraph_ingestor) == ["env"]

    def test_edgeless_resource_deleted(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        _seed(
            memgraph_ingestor,
            "CREATE (:Resource {qualified_name: 'stale', kind: 'ENDPOINT'})",
        )

        prune_unanchored_resources(memgraph_ingestor)

        assert _remaining(memgraph_ingestor) == []

    def test_flow_source_anchored_through_sink_survives(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # Regression: `fmt.Println(os.Getenv(...))` emits only the derived
        # ENV -FLOWS_TO-> STDOUT edge for the source, so the ENV resource
        # is live purely through its anchored sink.
        _seed(
            memgraph_ingestor,
            "CREATE (f:Function {qualified_name: 'p.main'}), "
            "(e:Resource {qualified_name: 'env', kind: 'ENV'}), "
            "(s:Resource {qualified_name: 'stdout', kind: 'STDOUT'}) "
            "CREATE (f)-[:WRITES_TO]->(s), (e)-[:FLOWS_TO]->(s)",
        )

        prune_unanchored_resources(memgraph_ingestor)

        assert _remaining(memgraph_ingestor) == ["env", "stdout"]

    def test_floating_flow_pair_deleted(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # A flow edge between two resources must not keep the pair alive
        # once the code that anchored either side is gone.
        _seed(
            memgraph_ingestor,
            "CREATE (e:Resource {qualified_name: 'env', kind: 'ENV'}), "
            "(s:Resource {qualified_name: 'stdout', kind: 'STDOUT'}) "
            "CREATE (e)-[:FLOWS_TO]->(s)",
        )

        prune_unanchored_resources(memgraph_ingestor)

        assert _remaining(memgraph_ingestor) == []

    def test_resolves_to_does_not_propagate_liveness(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # RESOLVES_TO is derived on every sync: it must neither anchor a
        # resource nor carry liveness from a live URL to a dead endpoint.
        _seed(
            memgraph_ingestor,
            "CREATE (f:Function {qualified_name: 'p.caller'}), "
            "(u:Resource {qualified_name: 'url', kind: 'NETWORK'}), "
            "(d:Resource {qualified_name: 'dead_ep', kind: 'ENDPOINT'}) "
            "CREATE (f)-[:READS_FROM]->(u), (u)-[:RESOLVES_TO]->(d)",
        )

        prune_unanchored_resources(memgraph_ingestor)

        assert _remaining(memgraph_ingestor) == ["url"]

    def test_empty_graph_is_noop(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        prune_unanchored_resources(memgraph_ingestor)

        assert _remaining(memgraph_ingestor) == []
