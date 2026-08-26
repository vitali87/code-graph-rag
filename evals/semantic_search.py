# Semantic-search relevance eval. cgr's semantic search embeds each function's
# source and retrieves by cosine similarity to a query embedding. This grades
# relevance directly: for fixtures whose natural-language query maps
# unambiguously to one function, does cgr's embedder rank that function in the
# top k? It uses cgr's own embedder over function source from the captured
# graph, so it tests cgr's embedding + ranking pipeline; the Qdrant ANN layer
# only approximates this same ranking.
from pathlib import Path
from typing import NamedTuple

from codebase_rag import constants as cs

from . import constants as ec
from .cgr_graph import _capture
from .score import _prf
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)


class SemanticCase(NamedTuple):
    query: str
    expected_qn: str


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def function_snippets(target: Path, project: str) -> dict[str, str]:
    # The source of every first-party function/method, keyed by qualified name,
    # read from the captured node's file and span, the same text cgr embeds.
    ingestor = _capture(target, project)
    snippets: dict[str, str] = {}
    for (label, uid), props in ingestor.nodes.items():
        if label not in (_FUNCTION, _METHOD):
            continue
        rel = props.get(cs.KEY_PATH)
        raw_start = props.get(cs.KEY_START_LINE)
        if not rel or not isinstance(raw_start, int | float):
            continue
        path = target / str(rel)
        if not path.is_file():
            continue
        start = int(raw_start)
        raw_end = props.get(cs.KEY_END_LINE)
        end = int(raw_end) if isinstance(raw_end, int | float) else start
        lines = path.read_text(encoding=cs.ENCODING_UTF8).splitlines()
        if start >= 1:
            snippets[str(uid)] = "\n".join(lines[start - 1 : end])
    return snippets


def cgr_semantic_ranking(
    target: Path, project: str, queries: list[str], top_k: int
) -> dict[str, list[str]]:
    from codebase_rag.embedder import embed_code_batch

    snippets = function_snippets(target, project)
    qns = list(snippets)
    snippet_vecs = embed_code_batch([snippets[qn] for qn in qns])
    query_vecs = embed_code_batch(queries)

    ranking: dict[str, list[str]] = {}
    for query, query_vec in zip(queries, query_vecs, strict=False):
        scored = sorted(
            (
                (qn, _cosine(query_vec, vec))
                for qn, vec in zip(qns, snippet_vecs, strict=False)
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        ranking[query] = [qn for qn, _score in scored[:top_k]]
    return ranking


def score_semantic(
    cases: list[SemanticCase], ranking: dict[str, list[str]]
) -> ScoreResult:
    # recall@k: a case is a hit when its expected function is in the query's
    # top-k. Modelled as a set of satisfied cases vs all cases, so precision is
    # 1.0 by construction and the headline number is recall.
    oracle = {(case.query, case.expected_qn) for case in cases}
    hits = {
        (case.query, case.expected_qn)
        for case in cases
        if case.expected_qn in ranking.get(case.query, [])
    }
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.SEMANTIC_LABEL, hits, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.SEMANTIC_DIFF_PREFIX + ec.SEMANTIC_LABEL] = DiffBucket(
            missing=[
                ec.SEMANTIC_CASE_REPR.format(query=q, expected=e)
                for q, e in sorted(oracle - hits)
            ],
            extra=[],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def proximity_edges(target: Path, project: str) -> dict[str, set[str]]:
    """Undirected adjacency between first-party definitions, keyed by qn.

    Built from the captured relationship tuples rather than a live graph, so
    the comparison runs in the same harness as the baseline and needs no
    Memgraph. Only the relationship types that mean "these two definitions are
    about the same thing" count -- see `tools/graph_rerank._PROXIMITY_RELS`.
    """
    from codebase_rag.tools.graph_rerank import _PROXIMITY_RELS

    ingestor = _capture(target, project)
    wanted = set(_PROXIMITY_RELS)
    adjacency: dict[str, set[str]] = {}
    for _from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type not in wanted:
            continue
        a, b = str(from_val), str(to_val)
        if a == b:
            continue
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    return adjacency


def reranked_semantic_ranking(
    ranking: dict[str, list[str]],
    adjacency: dict[str, set[str]],
    weight: float | None = None,
) -> dict[str, list[str]]:
    """The same rankings, reordered by in-set graph proximity (issue #385).

    Takes the BASELINE ranking as input rather than recomputing embeddings, so
    the two conditions differ by exactly one variable: the reranking step. Any
    difference in recall@k is attributable to it and to nothing else, which is
    what makes the comparison in criterion 3 meaningful.

    Scores are positional rather than cosine values: the baseline discards the
    similarity when it truncates to top-k, and a rank-derived score preserves
    the ordering that ranking expressed. Ties keep their baseline position
    because `rerank_by_graph_proximity` sorts stably.
    """
    from codebase_rag.tools.graph_rerank import (
        DEFAULT_PROXIMITY_WEIGHT,
        rerank_by_graph_proximity,
    )

    effective = DEFAULT_PROXIMITY_WEIGHT if weight is None else weight
    reranked: dict[str, list[str]] = {}
    for query, qns in ranking.items():
        if len(qns) < 2:
            reranked[query] = list(qns)
            continue
        index = {position: qn for position, qn in enumerate(qns)}
        in_set = set(qns)
        # Degree counted only against OTHER HITS for this query, matching the
        # reranker: an edge to some unrelated definition says nothing about
        # whether this hit belongs with the rest of the result set.
        degrees = {
            position: float(len(adjacency.get(qn, set()) & in_set))
            for position, qn in index.items()
        }
        highest = max(degrees.values(), default=0.0)
        # Descending positional similarity in 0..1, so first place scores 1.0.
        span = max(len(qns) - 1, 1)
        similarity = {position: 1.0 - (position / span) for position in index}
        ordered = rerank_by_graph_proximity(
            _AdjacencyIngestor(degrees, highest),
            [(position, similarity[position]) for position in index],
            weight=effective,
        )
        reranked[query] = [index[hit.node_id] for hit in ordered]
    return reranked


class _AdjacencyIngestor:
    """Feeds precomputed degrees to the reranker's query seam.

    The reranker asks the graph for in-set degree; here that answer is already
    known from the captured relationships, so this returns it rather than
    running Cypher. Keeping the reranker unmodified is the point -- the thing
    being measured must be the code that would ship.
    """

    def __init__(self, degrees: dict[int, float], highest: float) -> None:
        self._degrees = degrees
        self._highest = highest

    def fetch_all(self, query: str, params: dict | None = None) -> list[dict]:
        return [
            {"node_id": node_id, "degree": degree}
            for node_id, degree in self._degrees.items()
        ]

    def execute_write(self, query: str, params: dict | None = None) -> None:
        return None
