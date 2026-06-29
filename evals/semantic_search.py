# (H) Semantic-search relevance eval. cgr's semantic search embeds each function's
# (H) source and retrieves by cosine similarity to a query embedding. This grades
# (H) that relevance directly: for controlled fixtures whose natural-language query
# (H) maps unambiguously to one function, does cgr's embedder rank that function in
# (H) the top k? It uses cgr's own embedder over function source extracted from the
# (H) captured graph, so it tests cgr's embedding + ranking pipeline (the part that
# (H) decides relevance); the Qdrant ANN layer only approximates this same ranking.
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
    # (H) The source of every first-party function/method, keyed by qualified name,
    # (H) read from the captured node's file and span -- the same text cgr embeds.
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
    # (H) recall@k: a case is a hit when its expected function is in the query's
    # (H) top-k. Modelled as a set of satisfied cases vs all cases, so precision is
    # (H) 1.0 by construction and the headline number is recall.
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
