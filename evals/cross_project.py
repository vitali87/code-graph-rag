# Cross-project (monorepo) eval. Every other eval runs on a single top-level
# package, so none checks that cgr resolves references crossing top-level
# package boundaries, the monorepo case cgr is built for. This extracts cgr's
# CALLS and IMPORTS edges whose endpoints live in different top-level packages
# and grades them on synthetic fixtures whose cross edges are known by
# construction.
from pathlib import Path

from codebase_rag import constants as cs

from . import constants as ec
from .cgr_graph import _capture
from .score import _prf
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

_MODULE = cs.NodeLabel.MODULE.value
_CALLS = cs.RelationshipType.CALLS.value
_IMPORTS = cs.RelationshipType.IMPORTS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

Edge = tuple[str, str]


def _top_package(qn: str, project: str) -> str | None:
    # qn is project.<top-package>.<rest>; the top-level package is the segment
    # right after the project root. Bare project-level modules have none.
    parts = qn.split(cs.SEPARATOR_DOT)
    if len(parts) >= 3 and parts[0] == project:
        return parts[1]
    return None


def cgr_cross_package(target: Path, project: str) -> tuple[set[Edge], set[Edge]]:
    ingestor = _capture(target, project)
    calls: set[Edge] = set()
    imports: set[Edge] = set()
    for from_label, from_val, rel_type, to_label, to_val in ingestor.rels:
        src, dst = str(from_val), str(to_val)
        from_top = _top_package(src, project)
        to_top = _top_package(dst, project)
        if from_top is None or to_top is None or from_top == to_top:
            continue
        if rel_type == _CALLS:
            calls.add((src, dst))
        elif rel_type == _IMPORTS and from_label == _MODULE and to_label == _MODULE:
            imports.add((src, dst))
    return calls, imports


def _edge_repr(edge: Edge) -> str:
    return ec.CROSS_EDGE_REPR.format(src=edge[0], dst=edge[1])


def _bucket(cgr: set[Edge], oracle: set[Edge]) -> DiffBucket:
    return DiffBucket(
        missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
        extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
    )


def score_cross_project(
    cgr: tuple[set[Edge], set[Edge]], oracle: tuple[set[Edge], set[Edge]]
) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    for label, cgr_set, oracle_set in (
        (ec.CROSS_CALLS_LABEL, cgr[0], oracle[0]),
        (ec.CROSS_IMPORTS_LABEL, cgr[1], oracle[1]),
    ):
        row = _prf(ec.Category.EDGE.value, label, cgr_set, oracle_set)
        if row is not None:
            rows.append(row)
            diff[ec.CROSS_PROJECT_DIFF_PREFIX + label] = _bucket(cgr_set, oracle_set)
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)
