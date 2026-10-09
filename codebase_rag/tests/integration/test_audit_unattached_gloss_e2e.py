# Real-Memgraph check of issue #2652: the live audit `cgr doctor` runs must
# not count a LOST or AMBIGUOUS gloss, which has no ANNOTATES edge by design,
# as a node with no relationships. Only a real database proves the orphan
# Cypher filters on the anchor state.
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_audit import collect_live_violations

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def _orphan_details(ingestor: MemgraphIngestor) -> list[str]:
    return sorted(
        v.detail
        for v in collect_live_violations(ingestor.fetch_all)
        if v.check == cs.AuditCheck.ORPHAN_NODE
    )


def _gloss(ingestor: MemgraphIngestor, qn: str, state: str) -> None:
    ingestor._execute_query(
        "CREATE (:Gloss {qualified_name: $qn, anchor_state: $state})",
        {"qn": qn, "state": state},
    )


@pytest.mark.parametrize(
    "state", [cs.GlossAnchorState.LOST.value, cs.GlossAnchorState.AMBIGUOUS.value]
)
def test_an_unattached_gloss_passes_the_live_audit(
    memgraph_ingestor: MemgraphIngestor, state: str
) -> None:
    _gloss(memgraph_ingestor, "proj.note.1", state)

    assert _orphan_details(memgraph_ingestor) == []


# Negative: a gloss that should be attached, and any other orphan, still fail.


def test_an_exact_gloss_without_its_edge_and_a_bare_function_still_fail(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    _gloss(memgraph_ingestor, "proj.note.lost", cs.GlossAnchorState.LOST.value)
    _gloss(memgraph_ingestor, "proj.note.exact", cs.GlossAnchorState.EXACT.value)
    memgraph_ingestor._execute_query(
        "CREATE (:Function {qualified_name: 'proj.lib.f'})"
    )

    assert _orphan_details(memgraph_ingestor) == sorted(
        [
            cs.AUDIT_DETAIL_ORPHAN_COUNT.format(count=1, label="Gloss"),
            cs.AUDIT_DETAIL_ORPHAN_COUNT.format(count=1, label="Function"),
        ]
    )


def test_a_gloss_without_an_anchor_state_and_no_edge_still_fails(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    # `null IN [...]` is null, and so is `NOT (true AND null)`, which WHERE
    # drops: the state test must not skip a gloss that has none (bot review
    # on PR #2697).
    memgraph_ingestor._execute_query(
        "CREATE (:Gloss {qualified_name: 'proj.note.stateless'})"
    )

    assert _orphan_details(memgraph_ingestor) == [
        cs.AUDIT_DETAIL_ORPHAN_COUNT.format(count=1, label="Gloss")
    ]
