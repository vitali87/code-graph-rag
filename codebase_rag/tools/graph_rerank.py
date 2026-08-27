"""Graph-proximity reranking of semantic search results (issue #385).

Vector similarity ranks a result by how much its *text* resembles the query.
It knows nothing about how the codebase is wired, so two functions with
near-identical embeddings rank the same whether one sits at the centre of the
call graph and the other is an isolated leaf.

This reranks a result set by how connected each hit is to the others in the
same set. The idea: when several results cluster in one region of the graph,
that region is more likely to be what the query was about, and a hit wired
into the cluster is a better answer than an equally-similar hit outside it.

DELIBERATELY NOT INTEGRATED. Issue #385 makes integration conditional on a
measured retrieval-quality comparison, and no labelled retrieval corpus exists
in this repository yet. Wiring it into the query pipeline before that
measurement would smuggle in an assumption about a number nobody has taken.
This module is the prototype the measurement will be run against.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import constants as cs
from ..services import QueryProtocol

# How much graph proximity is allowed to move a result, as a fraction of the
# similarity score it is blended with. Deliberately small: vector similarity is
# the signal that found the result at all, and proximity is a tie-breaker
# between comparably-similar hits rather than a replacement ranking.
#
# The value is a starting point for the measurement, not a tuned constant --
# tuning it before there is a corpus to tune against would be fitting noise.
DEFAULT_PROXIMITY_WEIGHT = 0.25

# Edges that mean "these two definitions are about the same thing". DEFINES and
# DEFINES_METHOD are structural containment, CALLS and IMPORTS are use, INHERITS
# and OVERRIDES are type relationships. All are undirected for this purpose: a
# caller and its callee are equally in the same neighbourhood.
#
# CONTAINS_* is deliberately EXCLUDED, and the reason is that it could not
# contribute rather than that it is unwanted. Those five edges join
# Project/Folder/File/Module/Section nodes, never two Function or Method nodes
# — and proximity counts only edges whose BOTH endpoints are in the result set,
# which for semantic search is Function/Method. Adding them would change no
# score while implying a relationship the data cannot express.
#
# If the result set ever widens to include container nodes, revisit this: the
# exclusion is a consequence of what search returns, not a judgement about
# containment.
_PROXIMITY_RELS = (
    cs.RelationshipType.CALLS.value,
    cs.RelationshipType.DEFINES.value,
    cs.RelationshipType.DEFINES_METHOD.value,
    cs.RelationshipType.IMPORTS.value,
    cs.RelationshipType.INHERITS.value,
    cs.RelationshipType.OVERRIDES.value,
)


@dataclass(frozen=True)
class RerankedHit:
    """One result with its original and blended scores, both kept.

    The original is retained so a caller can report what reranking changed:
    a rerank that cannot be compared against its input is impossible to
    evaluate, which is the whole difficulty this issue is about.
    """

    node_id: int
    similarity: float
    proximity: float
    score: float


def build_proximity_query(node_ids: list[int]) -> str:
    """Count edges BETWEEN the given nodes, in either direction.

    Only edges whose both endpoints are in the result set count. An edge to
    some unrelated third node says nothing about whether this hit belongs with
    the others, and counting it would simply rank well-connected nodes highly
    for every query -- a popularity score, not a relevance one.

    Both endpoints are counted EXPLICITLY, via `UNWIND [id(a), id(b)]`, rather
    than relying on an undirected `MATCH` to yield each relationship once per
    orientation. That reliance is the subtle part: whether `(a)-[r]-(b)`
    enumerates a stored directed edge twice, so that both endpoints appear as
    `a` and both get counted, is an engine detail. If it does not, one
    endpoint of every directed edge silently goes unboosted, and the graph
    reranker then disagrees with the symmetric adjacency the eval builds --
    the two would diverge with nothing reporting it.

    Unwinding removes the dependency instead of documenting it. The result is
    the same where the undirected match does double-count, and correct where
    it does not.

    Self-loops are excluded (`id(a) <> id(b)`), matching the eval's adjacency
    which skips `a == b`. A recursive function calling only itself would
    otherwise be unwound TWICE and score maximum normalised proximity -- from
    a relationship that says nothing about whether it belongs with the other
    hits, which is the entire criterion. The exclusion is not defensive: it is
    what keeps the shipped reranker and the model it is measured against
    computing the same quantity.

    Each pair contributes once per DISTINCT relationship type, not once per
    edge (issue #1477). Two functions where one calls the other on three lines
    carry three CALLS edges, but that is one relationship observed three
    times: counting them separately would make proximity depend on how often a
    caller happens to invoke a callee, so a callee invoked in a loop body and
    again after it would outrank one invoked once from identical structure.
    Measured on this repo, that shape is the common one -- 285 of 1115
    adjacent pairs across three subtrees.

    Distinct TYPES are still counted separately, because they are separate
    relationships: an override that calls up (`super().handle()`) carries both
    CALLS and OVERRIDES, and is genuinely more strongly associated than a pair
    joined by either alone. `collect(DISTINCT type(r))` per pair before the
    UNWIND is what draws that line.
    """
    placeholders = ", ".join(f"${i}" for i in range(len(node_ids)))
    rel_filter = "|".join(_PROXIMITY_RELS)
    # Normalise each edge to an unordered (low, high) pair FIRST, so `a CALLS b`
    # and `b OVERRIDES a` land on the same pair and their types are deduplicated
    # together. Counting distinct types per ordered pair would miss that, and
    # the two orientations would each contribute their own count.
    return f"""
MATCH (a)-[r:{rel_filter}]->(b)
WHERE id(a) IN [{placeholders}] AND id(b) IN [{placeholders}] AND id(a) <> id(b)
WITH CASE WHEN id(a) < id(b) THEN id(a) ELSE id(b) END AS low,
     CASE WHEN id(a) < id(b) THEN id(b) ELSE id(a) END AS high,
     type(r) AS kind
WITH low, high, count(DISTINCT kind) AS kind_count
UNWIND [low, high] AS node_id
RETURN node_id, sum(kind_count) AS degree
"""


def _proximity_scores(ingestor: QueryProtocol, node_ids: list[int]) -> dict[int, float]:
    """Per-node in-set degree, normalised to 0..1 by the highest degree seen.

    Normalised against the SET rather than an absolute scale, because degree
    counts are not comparable across queries: a five-hit result in a dense
    module and a five-hit result across unrelated files would otherwise get
    wildly different proximity contributions for the same relative structure.
    """
    if len(node_ids) < 2:
        # A single hit has nothing to be proximate to; reranking one result is
        # a no-op and the query would be wasted work.
        return {}
    params = {str(i): node_id for i, node_id in enumerate(node_ids)}
    rows = ingestor.fetch_all(build_proximity_query(node_ids), params)
    degrees: dict[int, float] = {}
    for row in rows:
        node_id = row.get("node_id")
        degree = row.get("degree")
        if isinstance(node_id, int) and isinstance(degree, int | float):
            degrees[node_id] = float(degree)
    if not degrees:
        return {}
    highest = max(degrees.values())
    if highest <= 0:
        return {}
    return {node_id: degree / highest for node_id, degree in degrees.items()}


def rerank_by_graph_proximity(
    ingestor: QueryProtocol,
    results: list[tuple[int, float]],
    weight: float = DEFAULT_PROXIMITY_WEIGHT,
) -> list[RerankedHit]:
    """Blend vector similarity with in-set graph proximity.

    Order is preserved for ties so the output is deterministic: two hits with
    identical blended scores keep their vector-search order, and a rerank that
    shuffled equal scores would make any measurement irreproducible.

    A node with no in-set edges gets proximity 0 and keeps its similarity
    score unchanged rather than being penalised -- an isolated hit may still be
    the right answer, and this is a tie-breaker, not a filter.
    """
    if not results:
        return []
    proximity = _proximity_scores(ingestor, [node_id for node_id, _ in results])
    hits: list[RerankedHit] = []
    for node_id, similarity in results:
        prox = proximity.get(node_id, 0.0)
        hits.append(
            RerankedHit(
                node_id=node_id,
                similarity=similarity,
                proximity=prox,
                score=similarity + weight * prox,
            )
        )
    # `sorted` is stable, so equal scores keep their incoming order.
    return sorted(hits, key=lambda hit: hit.score, reverse=True)
