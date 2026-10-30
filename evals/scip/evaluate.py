"""Grade cgr's CALLS against a SCIP-derived call graph, per language.

Rows, at definition-site granularity (not file-level, not by simple name):

- `<language>`: cgr's edges against the call sites SCIP resolves, each to the
  target the indexer bound (the declared method for a virtual call);
- `<language>:dispatch`: the same, with every SCIP call widened to the
  overrides the index records (`is_implementation` relationships), the
  class-hierarchy call graph a dispatch-aware tool may also draw;
- `<language>:exact`: only the edges cgr bound `exact`, against the first row.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from ..cgr_graph import capture_calls
from ..score import _prf
from ..types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow
from . import constants as sc
from . import logs as sl
from .calls import oracle_edges
from .indexers import IndexerSpec, run_indexer
from .join import CgrEdges, cgr_edges, definition_repr
from .proto import read_index
from .types_defs import Definition, OracleEdges, ScipIndex

_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)
_Edge = tuple[Definition, Definition]


def _edge_repr(edge: _Edge) -> str:
    return sc.EDGE_REPR.format(
        caller=definition_repr(edge[0]), callee=definition_repr(edge[1])
    )


def score_calls(language: str, cgr: CgrEdges, oracle: OracleEdges) -> ScoreResult:
    rows: list[ScoreRow] = []
    for suffix, predicted, truth in (
        (sc.STRICT_SUFFIX, cgr.edges, oracle.edges),
        (sc.DISPATCH_SUFFIX, cgr.edges, oracle.dispatch_edges),
        (sc.EXACT_SUFFIX, cgr.exact, oracle.edges),
    ):
        if (row := _prf(sc.CATEGORY, language + suffix, predicted, truth)) is not None:
            rows.append(row)
    reached = {d for edge in oracle.edges for d in edge}
    diff: dict[str, DiffBucket] = {
        language: DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle.edges - cgr.edges)][
                : sc.DIFF_SAMPLE
            ],
            extra=[_edge_repr(e) for e in sorted(cgr.edges - oracle.dispatch_edges)][
                : sc.DIFF_SAMPLE
            ],
        ),
        sc.UNMATCHED_SCIP_KEY: DiffBucket(
            # Only callables that take part in a call: the rest cannot move
            # a score, so listing them would bury the ones that do.
            missing=[
                definition_repr(d)
                for d in sorted((oracle.callables - cgr.joined) & reached)
            ][: sc.DIFF_SAMPLE],
            extra=[],
        ),
        sc.UNMATCHED_CGR_KEY: DiffBucket(
            missing=[],
            extra=[definition_repr(d) for d in cgr.unmatched][: sc.DIFF_SAMPLE],
        ),
    }
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def load_index(
    spec: IndexerSpec, target: Path, project: str, index_path: Path | None
) -> ScipIndex:
    index = read_index(index_path) if index_path else run_indexer(spec, target, project)
    logger.info(
        sl.INDEX_READ.format(
            tool=index.tool, version=index.version, documents=len(index.documents)
        )
    )
    return index


def evaluate(
    spec: IndexerSpec, target: Path, project: str, index: ScipIndex
) -> ScoreResult:
    oracle = oracle_edges(index, target, spec)
    logger.success(
        sl.ORACLE_DONE.format(
            callables=len(oracle.callables),
            edges=len(oracle.edges),
            dispatch=len(oracle.dispatch_edges),
        )
    )
    logger.info(sl.CGR_RUNNING.format(target=target, project=project))
    ingestor = capture_calls(target, project)
    covered = frozenset(
        d.path for d in index.documents if d.path.endswith(spec.suffixes)
    )
    cgr = cgr_edges(ingestor, ingestor.calls, spec.suffixes, oracle.callables, covered)
    logger.success(
        sl.CGR_DONE.format(
            language=spec.language.value,
            edges=len(cgr.edges),
            unmatched=len(cgr.unmatched),
        )
    )
    return score_calls(spec.language.value, cgr, oracle)
