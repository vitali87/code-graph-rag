import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.constants import graph as cs
from evals.cgr_graph import _StatefulIngestor

_FN = cs.NodeLabel.FUNCTION.value
_MOD = cs.NodeLabel.MODULE.value
_EXT = cs.NodeLabel.EXTERNAL_MODULE.value
_QN = cs.KEY_QUALIFIED_NAME


def _node(store: _StatefulIngestor, label: str, qn: str) -> None:
    key = cs.NODE_UNIQUE_CONSTRAINTS[label]
    store.ensure_node_batch(label, {key: qn, _QN: qn})


def _edge(
    store: _StatefulIngestor, src: tuple[str, str], rel: str, dst: tuple[str, str]
) -> None:
    store.ensure_relationship_batch(
        (src[0], cs.NODE_UNIQUE_CONSTRAINTS[src[0]], src[1]),
        rel,
        (dst[0], cs.NODE_UNIQUE_CONSTRAINTS[dst[0]], dst[1]),
    )


def _names(store: _StatefulIngestor, query: str, qn: str) -> set[str]:
    rows = store.fetch_all(query, {cs.KEY_QN: qn, cs.KEY_PROJECT_PREFIX: "proj."})
    return {str(row[_QN]) for row in rows}


@pytest.mark.parametrize(
    ("query", "rel", "outgoing"),
    [
        (cq.CYPHER_GRAPH_CALLEES, cs.RelationshipType.CALLS.value, True),
        (cq.CYPHER_GRAPH_CALLERS, cs.RelationshipType.CALLS.value, False),
        (cq.CYPHER_GRAPH_REFERENCES, cs.RelationshipType.REFERENCES.value, False),
        (cq.CYPHER_GRAPH_TYPE_EDGES, cs.RelationshipType.INHERITS.value, False),
        (cq.CYPHER_GRAPH_OVERRIDES, cs.RelationshipType.OVERRIDES.value, True),
        (cq.CYPHER_GRAPH_OVERRIDES, cs.RelationshipType.OVERRIDES.value, False),
    ],
)
def test_a_neighbour_outside_the_project_is_not_returned(
    query: str, rel: str, outgoing: bool
) -> None:
    """Every graph read filters the far endpoint with `STARTS WITH
    $project_prefix`; the emulator returned the out-of-project neighbour
    too, so a test on it passed on rows production never yields (#1604)."""
    store = _StatefulIngestor()
    me, mine, theirs = "proj.a.f", "proj.b.g", "vendor.lib.h"
    for qn in (me, mine, theirs):
        _node(store, _FN, qn)
    for other in (mine, theirs):
        pair = ((_FN, me), (_FN, other)) if outgoing else ((_FN, other), (_FN, me))
        _edge(store, pair[0], rel, pair[1])
    assert _names(store, query, me) == {mine}


def test_an_importer_outside_the_project_is_not_returned() -> None:
    store = _StatefulIngestor()
    target = "proj.util"
    for qn in (target, "proj.app", "other.app"):
        _node(store, _MOD, qn)
    for importer in ("proj.app", "other.app"):
        _edge(
            store, (_MOD, importer), cs.RelationshipType.IMPORTS.value, (_MOD, target)
        )
    assert _names(store, cq.CYPHER_GRAPH_IMPORTERS, target) == {"proj.app"}


def test_an_external_callee_is_not_returned() -> None:
    """The case the issue names: a project function calling a dependency."""
    store = _StatefulIngestor()
    _node(store, _FN, "proj.a.f")
    _node(store, _EXT, "requests")
    _edge(store, (_FN, "proj.a.f"), cs.RelationshipType.CALLS.value, (_EXT, "requests"))
    assert _names(store, cq.CYPHER_GRAPH_CALLEES, "proj.a.f") == set()


def test_a_type_outside_the_project_is_not_a_context_type() -> None:
    """`CYPHER_CONTEXT_TYPES` filters the type by the project prefix too."""
    store = _StatefulIngestor()
    cls = cs.NodeLabel.CLASS.value
    _node(store, _FN, "proj.a.f")
    for qn in ("proj.m.Mine", "vendor.m.Theirs"):
        _node(store, cls, qn)
        _edge(store, (_FN, "proj.a.f"), cs.RelationshipType.RETURNS.value, (cls, qn))
    assert _names(store, cq.CYPHER_CONTEXT_TYPES, "proj.a.f") == {"proj.m.Mine"}
