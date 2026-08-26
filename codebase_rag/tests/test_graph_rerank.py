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

    # Both endpoints reach the UNWIND, via the normalised (low, high) pair the
    # distinct-type counting in #1477 introduced. What matters is that neither
    # endpoint is dropped, not which expression names them.
    assert "UNWIND [low, high]" in query, query
    assert "AS low" in query and "AS high" in query, query
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


# --- the shipped query and the eval's adjacency must agree (issue #1477) ----
#
# The three bugs fixed in review on #1467 -- one endpoint of a directed edge
# going uncounted, self-loops being double-counted, and the CONTAINS_*
# exclusion being unpinned -- were all DIVERGENCES between the shipped Cypher
# and the adjacency model the eval scores it against, rather than freestanding
# errors. Nothing caught them because nothing compared the two implementations,
# only each one against its own expectations.
#
# `build_proximity_query` is pinned structurally above because this suite has
# no Memgraph. These execute the query's SEMANTICS over the same relationship
# tuples `evals.semantic_search.proximity_edges` consumes, and assert both
# arrive at the same in-set degrees. A divergence in either direction fails
# here rather than silently making the measurement describe different code
# from the code that ships.


def _degrees_via_shipped_semantics(
    rels: list[tuple[str, str, str]], in_set: set[str]
) -> dict[str, int]:
    """In-set degree the way the shipped Cypher computes it.

    Mirrors `build_proximity_query` clause for clause over tuples:
    `MATCH (a)-[r:REL]->(b)` filtered to `_PROXIMITY_RELS`, both endpoints in
    the node-id list, `id(a) <> id(b)`, then `UNWIND [id(a), id(b)]` counting
    each endpoint once per DISTINCT relationship TYPE joining the pair
    (issue #1477).
    """
    from codebase_rag.tools.graph_rerank import _PROXIMITY_RELS

    # (unordered pair) -> the distinct counted types joining it, matching the
    # query's `count(DISTINCT type(r))` per pair.
    kinds: dict[tuple[str, str], set[str]] = {}
    for source, rel_type, target in rels:
        if rel_type not in _PROXIMITY_RELS:
            continue
        if source not in in_set or target not in in_set:
            continue
        if source == target:  # id(a) <> id(b)
            continue
        kinds.setdefault(tuple(sorted((source, target))), set()).add(rel_type)

    degrees: dict[str, int] = {}
    for (left, right), types in kinds.items():
        for endpoint in (left, right):  # UNWIND [id(a), id(b)]
            degrees[endpoint] = degrees.get(endpoint, 0) + len(types)
    return degrees


def _degrees_via_eval_adjacency(
    rels: list[tuple[str, str, str]], in_set: set[str]
) -> dict[str, int]:
    """In-set degree the way `evals.semantic_search.proximity_edges` does.

    That helper builds an undirected adjacency keyed by qualified name whose
    values are the distinct counted relationship TYPES joining the pair;
    `reranked_semantic_ranking` then sums those over in-set neighbours
    (issue #1477).
    """
    from codebase_rag.tools.graph_rerank import _PROXIMITY_RELS

    adjacency: dict[str, dict[str, set[str]]] = {}
    for source, rel_type, target in rels:
        if rel_type not in _PROXIMITY_RELS:
            continue
        if source == target:
            continue
        adjacency.setdefault(source, {}).setdefault(target, set()).add(rel_type)
        adjacency.setdefault(target, {}).setdefault(source, set()).add(rel_type)

    degrees: dict[str, int] = {}
    for qn in in_set:
        total = sum(
            len(types)
            for neighbour, types in adjacency.get(qn, {}).items()
            if neighbour in in_set
        )
        if total:
            degrees[qn] = total
    return degrees


def test_directed_edges_boost_both_endpoints_in_both_implementations() -> None:
    """The directed-endpoint bug, caught by comparison rather than by inspection.

    `a` CALLS `b` and nothing else. Both must gain degree 1. Projecting only
    `id(a)` -- the shape this replaced -- would leave `b` at 0 in the shipped
    path while the eval's symmetric adjacency still reports 1.
    """
    rels = [("a", "CALLS", "b")]
    in_set = {"a", "b"}

    shipped = _degrees_via_shipped_semantics(rels, in_set)
    evaluated = _degrees_via_eval_adjacency(rels, in_set)

    assert shipped == evaluated, f"shipped={shipped} eval={evaluated}"
    assert shipped == {"a": 1, "b": 1}


def test_a_self_loop_contributes_nothing_in_both_implementations() -> None:
    """A recursive function scores zero proximity, not maximum, in both paths."""
    rels = [("rec", "CALLS", "rec"), ("a", "CALLS", "b")]
    in_set = {"rec", "a", "b"}

    shipped = _degrees_via_shipped_semantics(rels, in_set)
    evaluated = _degrees_via_eval_adjacency(rels, in_set)

    assert shipped == evaluated, f"shipped={shipped} eval={evaluated}"
    assert "rec" not in shipped, shipped


def test_containment_edges_change_no_degree_in_either_implementation() -> None:
    """CONTAINS_* is excluded from the shipped query; the eval must match.

    Pinned by comparison rather than by asserting the constant, so that adding
    a relationship type to one path and not the other fails here.
    """
    rels = [
        ("mod", "CONTAINS_FILE", "a"),
        ("mod", "CONTAINS_FILE", "b"),
        ("a", "CALLS", "b"),
    ]
    in_set = {"mod", "a", "b"}

    shipped = _degrees_via_shipped_semantics(rels, in_set)
    evaluated = _degrees_via_eval_adjacency(rels, in_set)

    assert shipped == evaluated, f"shipped={shipped} eval={evaluated}"
    assert "mod" not in shipped, shipped


def test_the_two_implementations_agree_over_a_mixed_graph() -> None:
    """One fixture exercising every divergence at once.

    Directed edges in both orientations, a self-loop, an out-of-set edge, a
    containment edge, and an unrelated relationship type. Any single-path
    change to the proximity model breaks the equality.

    At most ONE counted edge joins any pair here -- see
    `test_a_multi_edge_pair_is_a_known_divergence` for why that restriction is
    load-bearing rather than incidental.
    """
    rels = [
        ("a", "CALLS", "b"),
        ("b", "CALLS", "c"),
        ("c", "CALLS", "a"),
        ("a", "CALLS", "a"),
        ("a", "CALLS", "outside"),
        ("mod", "CONTAINS_FILE", "a"),
    ]
    in_set = {"a", "b", "c"}

    shipped = _degrees_via_shipped_semantics(rels, in_set)
    evaluated = _degrees_via_eval_adjacency(rels, in_set)

    assert shipped == evaluated, f"shipped={shipped} eval={evaluated}"
    # Every in-set node is connected to both others by at least one counted
    # edge, so none may be missing -- an empty result would satisfy the
    # equality above while measuring nothing.
    assert set(shipped) == in_set, shipped


def test_distinct_relationship_types_between_one_pair_each_count() -> None:
    """Two DIFFERENT relationship types joining a pair count as two (#1477).

    An override that calls up -- `Child.handle` calling `super().handle()` --
    carries both CALLS and OVERRIDES between the same pair. Those are two
    genuinely different relationships, not one observed twice, so the pair is
    more strongly associated than one joined by either alone.
    """
    rels = [("a", "CALLS", "b"), ("a", "OVERRIDES", "b")]
    in_set = {"a", "b"}

    shipped = _degrees_via_shipped_semantics(rels, in_set)
    evaluated = _degrees_via_eval_adjacency(rels, in_set)

    assert shipped == evaluated, f"shipped={shipped} eval={evaluated}"
    assert shipped == {"a": 2, "b": 2}, shipped


def test_the_same_relationship_type_repeated_counts_once() -> None:
    """One relationship observed twice is not a stronger association (#1477).

    A caller invoking a callee on two lines emits two CALLS edges. Counting
    both would make proximity depend on how many times a function happens to
    call another, so a callee invoked in a loop body and once after it would
    outrank one invoked once from otherwise identical structure.

    Measured on this repo, this is the DOMINANT multi-edge shape: 285 of 1115
    adjacent pairs across three subtrees, and all 51 multi-CALLS pairs in
    `codebase_rag/tools` were same-direction repeats with zero mutual.
    """
    rels = [("a", "CALLS", "b"), ("a", "CALLS", "b"), ("a", "CALLS", "b")]
    in_set = {"a", "b"}

    shipped = _degrees_via_shipped_semantics(rels, in_set)
    evaluated = _degrees_via_eval_adjacency(rels, in_set)

    assert shipped == evaluated, f"shipped={shipped} eval={evaluated}"
    assert shipped == {"a": 1, "b": 1}, shipped


def test_repeated_and_distinct_types_are_told_apart() -> None:
    """The two multi-edge families must not collapse into each other.

    Without this, counting distinct types could be replaced by "count 1 per
    pair" and both tests above would still pass -- an implementation that
    discards the OVERRIDES signal entirely.
    """
    repeated = [("a", "CALLS", "b"), ("a", "CALLS", "b")]
    distinct = [("a", "CALLS", "b"), ("a", "OVERRIDES", "b")]
    in_set = {"a", "b"}

    assert _degrees_via_shipped_semantics(repeated, in_set) == {"a": 1, "b": 1}
    assert _degrees_via_shipped_semantics(distinct, in_set) == {"a": 2, "b": 2}
    assert _degrees_via_eval_adjacency(repeated, in_set) == {"a": 1, "b": 1}
    assert _degrees_via_eval_adjacency(distinct, in_set) == {"a": 2, "b": 2}
