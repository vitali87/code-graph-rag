"""Liveness rules for shared Resource nodes (dogfood findings on #425).

``delete-project`` and incremental rebuilds strip edges off prefix-less
Resource nodes without deleting them; cleanup must remove exactly the
resources whose component no longer reaches a code node.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from codebase_rag.services.resource_cleanup import (
    CYPHER_ALL_RESOURCE_QNS,
    CYPHER_ANCHORED_RESOURCE_QNS,
    CYPHER_DELETE_RESOURCES_BY_QN,
    CYPHER_RESOURCE_ADJACENCY,
    prune_unanchored_resources,
)


def _ingestor(
    all_qns: list[str],
    anchored: list[str],
    adjacency: list[tuple[str, str]],
) -> MagicMock:
    responses = {
        CYPHER_ALL_RESOURCE_QNS: [{"qualified_name": qn} for qn in all_qns],
        CYPHER_ANCHORED_RESOURCE_QNS: [{"qualified_name": qn} for qn in anchored],
        CYPHER_RESOURCE_ADJACENCY: [{"a": a, "b": b} for a, b in adjacency],
    }
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = lambda query, params=None: responses[query]
    return ingestor


def _deleted(ingestor: MagicMock) -> list[str]:
    calls = [
        c
        for c in ingestor.execute_write.call_args_list
        if c.args[0] == CYPHER_DELETE_RESOURCES_BY_QN
    ]
    if not calls:
        return []
    (qns,) = [c.args[1]["qns"] for c in calls]
    return list(qns)


class TestPruneUnanchoredResources:
    def test_anchored_resource_survives(self) -> None:
        ingestor = _ingestor(["resource::ENV::HOME"], ["resource::ENV::HOME"], [])

        assert prune_unanchored_resources(ingestor) == 0
        assert _deleted(ingestor) == []

    def test_edgeless_resource_deleted(self) -> None:
        ingestor = _ingestor(["resource::ENDPOINT::GET /users"], [], [])

        assert prune_unanchored_resources(ingestor) == 1
        assert _deleted(ingestor) == ["resource::ENDPOINT::GET /users"]

    def test_flow_source_anchored_through_sink_survives(self) -> None:
        # Regression: `fmt.Println(os.Getenv(...))` emits only the derived
        # ENV -FLOWS_TO-> STDOUT edge for the source, so the ENV resource
        # is live purely through its anchored sink.
        env, stdout = "resource::ENV::SECRET", "resource::STDOUT::<dynamic>"
        ingestor = _ingestor([env, stdout], [stdout], [(env, stdout)])

        assert prune_unanchored_resources(ingestor) == 0
        assert _deleted(ingestor) == []

    def test_floating_flow_pair_deleted(self) -> None:
        # A flow edge between two resources must not keep the pair alive
        # once the code that anchored either side is gone.
        env, stdout = "resource::ENV::SECRET", "resource::STDOUT::<dynamic>"
        ingestor = _ingestor([env, stdout], [], [(env, stdout)])

        assert prune_unanchored_resources(ingestor) == 2
        assert _deleted(ingestor) == sorted([env, stdout])

    def test_liveness_only_propagates_through_flow_edges(self) -> None:
        # RESOLVES_TO is derived on every sync: it must neither anchor a
        # resource nor carry liveness from a live URL to a dead endpoint.
        assert ":FLOWS_TO" in CYPHER_RESOURCE_ADJACENCY
        assert "RESOLVES_TO" not in CYPHER_RESOURCE_ADJACENCY

    def test_mixed_component_keeps_only_reachable(self) -> None:
        env, stdout = "resource::ENV::SECRET", "resource::STDOUT::<dynamic>"
        stale = "resource::ENDPOINT::GET /old"
        ingestor = _ingestor([env, stdout, stale], [stdout], [(env, stdout)])

        assert prune_unanchored_resources(ingestor) == 1
        assert _deleted(ingestor) == [stale]

    def test_empty_graph_is_noop(self) -> None:
        ingestor = _ingestor([], [], [])

        assert prune_unanchored_resources(ingestor) == 0
        ingestor.execute_write.assert_not_called()
