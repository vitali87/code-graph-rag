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

# Edges that mean "these two definitions are about the same thing". CONTAINS_*
# and DEFINES are structural containment, CALLS and IMPORTS are use, INHERITS
# and OVERRIDES are type relationships. All are undirected for this purpose:
# a caller and its callee are equally in the same neighbourhood.
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
    """
    placeholders = ", ".join(f"${i}" for i in range(len(node_ids)))
    rel_filter = "|".join(_PROXIMITY_RELS)
    return f"""
MATCH (a)-[r:{rel_filter}]-(b)
WHERE id(a) IN [{placeholders}] AND id(b) IN [{placeholders}]
RETURN id(a) AS node_id, count(r) AS degree
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
