# Graph-proximity reranking prototype (issue #385).
#
# The prototype is deliberately not wired into the query pipeline: #385 makes
# integration conditional on a measured retrieval-quality comparison, and no
# labelled retrieval corpus exists yet. These tests pin the scoring behaviour
# so the measurement has something stable to run against.
from __future__ import annotations

from unittest.mock import MagicMock

from codebase_rag.tools.graph_rerank import (
    DEFAULT_PROXIMITY_WEIGHT,
    build_proximity_query,
    rerank_by_graph_proximity,
)


def _ingestor(degrees: dict[int, int]) -> MagicMock:
    mock = MagicMock()
    mock.fetch_all.return_value = [
        {"node_id": node_id, "degree": degree} for node_id, degree in degrees.items()
    ]
    return mock


def test_a_connected_hit_outranks_an_equally_similar_isolated_one() -> None:
    """The whole premise: proximity breaks ties vector similarity cannot.

    Both hits have identical similarity, so any reordering can only come from
    the graph. Without identical similarity the test would pass on the vector
    score alone and prove nothing about reranking.
    """
    ingestor = _ingestor({1: 4, 2: 0})

    ranked = rerank_by_graph_proximity(ingestor, [(2, 0.8), (1, 0.8)])

    assert [hit.node_id for hit in ranked] == [1, 2]


def test_similarity_still_dominates_a_large_gap() -> None:
    """Proximity is a tie-breaker, not a replacement ranking.

    A far-more-similar but isolated hit must stay on top, or the reranker is
    overriding the signal that found the results in the first place. This is
    the paired control for the test above: together they pin proximity as
    influential but bounded.
    """
    ingestor = _ingestor({1: 10, 2: 0})

    ranked = rerank_by_graph_proximity(ingestor, [(2, 0.95), (1, 0.60)])

    assert ranked[0].node_id == 2, [(h.node_id, h.score) for h in ranked]


def test_an_isolated_hit_keeps_its_similarity_unchanged() -> None:
    """No in-set edges means no penalty -- an isolated hit may still be right."""
    ingestor = _ingestor({1: 3})

    ranked = rerank_by_graph_proximity(ingestor, [(1, 0.7), (2, 0.5)])

    isolated = next(hit for hit in ranked if hit.node_id == 2)
    assert isolated.proximity == 0.0
    assert isolated.score == 0.5


def test_the_original_similarity_is_retained() -> None:
    """A rerank that cannot be compared against its input cannot be evaluated.

    #385 requires comparing retrieval quality with and without reranking, so
    discarding the pre-rerank score would make the issue's own acceptance
    criterion unmeasurable.
    """
    ingestor = _ingestor({1: 2, 2: 1})

    ranked = rerank_by_graph_proximity(ingestor, [(1, 0.9), (2, 0.4)])

    assert {hit.node_id: hit.similarity for hit in ranked} == {1: 0.9, 2: 0.4}


def test_a_single_result_skips_the_graph_query_entirely() -> None:
    """One hit has nothing to be proximate to, so the query is wasted work."""
    ingestor = _ingestor({})

    ranked = rerank_by_graph_proximity(ingestor, [(1, 0.9)])

    ingestor.fetch_all.assert_not_called()
    assert [hit.node_id for hit in ranked] == [1]


def test_no_results_is_a_no_op() -> None:
    ingestor = _ingestor({})

    assert rerank_by_graph_proximity(ingestor, []) == []
    ingestor.fetch_all.assert_not_called()


def test_equal_scores_keep_their_incoming_order() -> None:
    """Determinism: an irreproducible rerank cannot be measured.

    With no edges at all every blended score is the similarity, so the output
    must be the input order rather than an arbitrary shuffle.
    """
    ingestor = _ingestor({})

    ranked = rerank_by_graph_proximity(ingestor, [(3, 0.5), (1, 0.5), (2, 0.5)])

    assert [hit.node_id for hit in ranked] == [3, 1, 2]


def test_the_query_counts_only_edges_inside_the_result_set() -> None:
    """Both endpoints must be in the set, or this ranks popularity not relevance.

    An edge to an unrelated third node says nothing about whether a hit belongs
    with the others; counting it would rank well-connected nodes highly for
    every query regardless of what was asked.
    """
    query = build_proximity_query([1, 2, 3])

    assert query.count("id(a) IN") == 1
    assert query.count("id(b) IN") == 1
    assert "AND" in query


def test_the_weight_bounds_how_far_proximity_can_move_a_result() -> None:
    """A control on the constant: proximity is capped at `weight`.

    Normalisation puts proximity in 0..1, so the largest possible boost is the
    weight itself. Asserting it keeps a future tuning change from silently
    letting the graph override similarity.
    """
    ingestor = _ingestor({1: 5})

    ranked = rerank_by_graph_proximity(ingestor, [(1, 0.5), (2, 0.5)])

    boosted = next(hit for hit in ranked if hit.node_id == 1)
    assert boosted.score == 0.5 + DEFAULT_PROXIMITY_WEIGHT


# A REACHABLE hub fixture: three hits, where node 1 is joined to both 2 and 3
# and 2-3 are not joined to each other. `build_proximity_query` unwinds both
# endpoints of every in-set edge, so the two edges 1-2 and 1-3 give:
#
#     degree: {1: 2, 2: 1, 3: 1}   normalised: {1: 1.0, 2: 0.5, 3: 0.5}
#
# Three nodes is the MINIMUM that can produce unequal degrees. With a two-node
# result set every matched edge runs between those two nodes and UNWIND emits
# both endpoints, so their degrees are necessarily equal -- an asymmetric
# two-node fixture like `{1: 1, 2: 4}` describes a graph the production query
# cannot return, which is what an earlier version of these tests asserted
# against (caught in review on #1482).
_HUB_DEGREES = {1: 2, 2: 1, 3: 1}


def test_proximity_is_proportional_to_degree_not_mere_connectedness() -> None:
    """Proximity must scale with degree, not with the yes/no fact of an edge.

    The module ranks a hit by HOW connected it is to the rest of the result
    set. An implementation giving every node with any in-set edge the same
    flat boost ranks by WHETHER it is connected -- a different quantity, and
    the same membership-vs-degree confusion as #1474.

    Every earlier fixture in this file used one connected node and one
    isolated node, where the connected node's normalised degree is 1.0 and the
    flat boost is also 1.0. The two implementations agree on every such case,
    so the whole suite passed against flat boost (issue #1481).

    Asserts PROXIMITY rather than rank order, because proximity is computed
    before `weight` is applied and is therefore weight-independent:

        degree-weighted: {1: 1.0, 2: 0.5, 3: 0.5}
        flat boost:      {1: 1.0, 2: 1.0, 3: 1.0}

    An earlier version asserted the resulting ORDER instead, and claimed in
    this docstring that doing so kept the test meaningful across a retune of
    DEFAULT_PROXIMITY_WEIGHT. That was exactly backwards: a blended score
    depends on the weight, and below w =~ 0.133 the ordering no longer
    separated the implementations at all. The claim was disproved by
    execution in review.
    """
    ingestor = _ingestor(_HUB_DEGREES)

    ranked = rerank_by_graph_proximity(ingestor, [(1, 0.5), (2, 0.5), (3, 0.5)])
    proximity = {hit.node_id: hit.proximity for hit in ranked}

    assert proximity == {1: 1.0, 2: 0.5, 3: 0.5}, proximity


def test_a_less_connected_hit_is_boosted_strictly_less() -> None:
    """The consequence of the above, pinned independently of the weight.

    With equal similarities the entire score gap comes from proximity, so the
    hub must outrank the spokes for ANY positive weight -- whereas flat boost
    gives all three the same proximity and leaves them tied, preserving the
    incoming order under a stable sort.

    Pairs with the test above: that one pins the mechanism, this one pins the
    behaviour a caller actually observes.
    """
    ingestor = _ingestor(_HUB_DEGREES)

    ranked = rerank_by_graph_proximity(ingestor, [(2, 0.5), (3, 0.5), (1, 0.5)])

    assert ranked[0].node_id == 1, [
        (h.node_id, h.proximity, h.score) for h in ranked
    ]
    assert ranked[0].score > ranked[1].score, [
        (h.node_id, h.score) for h in ranked
    ]


def test_containment_edges_are_excluded_deliberately() -> None:
    """CONTAINS_* cannot contribute, so its absence is a fact not an oversight.

    Those five edges join Project/Folder/File/Module/Section nodes, never two
    Function or Method nodes. Proximity counts only edges whose BOTH endpoints
    are in the result set, and semantic search returns Function/Method, so
    including them would change no score while implying a relationship the
    data cannot express.

    Pinned rather than commented: a reader seeing five declared CONTAINS_*
    values absent from the filter cannot otherwise tell "deliberately omitted"
    from "forgotten", and the safe-looking change is to add them.
    """
    from codebase_rag import constants as cs
    from codebase_rag.tools.graph_rerank import _PROXIMITY_RELS

    containment = {
        member.value
        for member in cs.RelationshipType
        if member.value.startswith("CONTAINS_")
    }

    assert containment, "no CONTAINS_* values found; the guard has nothing to check"
    assert not containment & set(_PROXIMITY_RELS), sorted(
        containment & set(_PROXIMITY_RELS)
    )


def test_the_proximity_query_filters_on_the_declared_relationships() -> None:
    """The query must use exactly `_PROXIMITY_RELS`, not a drifted subset.

    The tuple and the generated Cypher are two representations of one
    decision; if they diverge, the constant documents something the query does
    not do.

    Compares the SET extracted from the `r:` pattern, not membership of each
    declared value. An earlier version asserted only that every declared
    relationship appeared somewhere in the query text, which still passed if
    the query filtered on an UNDECLARED one -- including a `CONTAINS_*` edge,
    the exact thing the sibling test exists to keep out. Containment answers
    "are the declared ones present"; the question is "are these exactly the
    ones used".
    """
    import re

    from codebase_rag.tools.graph_rerank import _PROXIMITY_RELS, build_proximity_query

    query = build_proximity_query([1, 2])

    match = re.search(r"\[r:([^\]]+)\]", query)
    assert match is not None, query

    assert set(match.group(1).split("|")) == set(_PROXIMITY_RELS), match.group(1)


def test_the_query_counts_both_endpoints_explicitly() -> None:
    """A directed edge must boost BOTH of its endpoints.

    The reranker treats proximity as undirected, and the eval builds a
    symmetric adjacency to match. The Cypher must agree without depending on
    whether an undirected `MATCH` enumerates a stored directed edge once or
    twice -- an engine detail. If it enumerates once, one endpoint of every
    directed edge goes unboosted and the shipped reranker silently disagrees
    with the model it was measured against.

    Pinned structurally because there is no Memgraph in this suite: asserting
    the query unwinds both ids is the strongest available check, and it is the
    property that makes the engine detail irrelevant.
    """
    from codebase_rag.tools.graph_rerank import build_proximity_query

    query = build_proximity_query([1, 2])

    assert "UNWIND [id(a), id(b)]" in query, query
    # A bare undirected match with a single projected endpoint is the shape
    # this replaced; it must not come back.
    assert "RETURN id(a) AS node_id" not in query, query


def test_self_loops_are_excluded_from_proximity() -> None:
    """A function calling itself is not "close to" anything.

    The eval's adjacency skips `a == b`, so the query must too. Without the
    exclusion a recursive function is unwound TWICE by
    `UNWIND [id(a), id(b)]` and scores maximum normalised proximity purely
    from its own self-reference -- outranking hits that are genuinely
    connected to the rest of the result set.

    That would also make the shipped reranker and the model it is measured
    against compute different quantities, which is worse than either being
    wrong: the measurement stops describing the thing being shipped.
    """
    from codebase_rag.tools.graph_rerank import build_proximity_query

    query = build_proximity_query([1, 2])

    assert "id(a) <> id(b)" in query, query
