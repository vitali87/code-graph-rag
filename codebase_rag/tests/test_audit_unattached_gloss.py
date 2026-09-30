"""Issue #2652: an unattached gloss is a retained state, not an orphan.

A gloss whose definition is gone keeps its note with no ANNOTATES edge:
LOST when nothing carries its hash, AMBIGUOUS when several do (#1808). The
structural audit counted it as a node with no relationships, so `cgr doctor`
failed permanently after an annotated function was removed or rewritten.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_audit import collect_violations
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord

_GLOSS = cs.NodeLabel.GLOSS.value


def _gloss(qn: str, state: str | None) -> GraphNodeRecord:
    props = {cs.KEY_QUALIFIED_NAME: qn}
    if state is not None:
        props[cs.KEY_ANCHOR_STATE] = state
    return GraphNodeRecord(_GLOSS, props)


def _graph(
    *glosses: GraphNodeRecord,
) -> tuple[list[GraphNodeRecord], list[GraphRelRecord]]:
    project = GraphNodeRecord(cs.NodeLabel.PROJECT.value, {cs.KEY_NAME: "proj"})
    module = GraphNodeRecord(
        cs.NodeLabel.MODULE.value, {cs.KEY_QUALIFIED_NAME: "proj.lib"}
    )
    rels = [
        GraphRelRecord(
            (cs.NodeLabel.PROJECT.value, cs.KEY_NAME, "proj"),
            cs.RelationshipType.CONTAINS_MODULE.value,
            (cs.NodeLabel.MODULE.value, cs.KEY_QUALIFIED_NAME, "proj.lib"),
        )
    ]
    return [project, module, *glosses], rels


def _orphans(nodes: list[GraphNodeRecord], rels: list[GraphRelRecord]) -> list[str]:
    return [
        v.detail
        for v in collect_violations(nodes, rels)
        if v.check == cs.AuditCheck.ORPHAN_NODE
    ]


@pytest.mark.parametrize(
    "state", [cs.GlossAnchorState.LOST.value, cs.GlossAnchorState.AMBIGUOUS.value]
)
def test_an_unattached_gloss_is_no_orphan(state: str) -> None:
    assert _orphans(*_graph(_gloss("proj.note.1", state))) == []


# Negative: what must not change.


@pytest.mark.parametrize(
    "state",
    [
        cs.GlossAnchorState.EXACT.value,
        cs.GlossAnchorState.MOVED.value,
        cs.GlossAnchorState.STALE.value,
        None,
    ],
)
def test_a_gloss_that_should_be_attached_but_is_not_is_still_an_orphan(
    state: str | None,
) -> None:
    assert _orphans(*_graph(_gloss("proj.note.1", state))) == [
        cs.AUDIT_DETAIL_ORPHAN.format(label=_GLOSS, key="proj.note.1")
    ]


def test_any_other_orphan_is_still_reported() -> None:
    nodes, rels = _graph(_gloss("proj.note.1", cs.GlossAnchorState.LOST.value))
    nodes.append(
        GraphNodeRecord(
            cs.NodeLabel.FUNCTION.value, {cs.KEY_QUALIFIED_NAME: "proj.lib.f"}
        )
    )

    assert _orphans(nodes, rels) == [
        cs.AUDIT_DETAIL_ORPHAN.format(
            label=cs.NodeLabel.FUNCTION.value, key="proj.lib.f"
        )
    ]
