"""Shared machinery for the per-language call-retrieval evals.

Every evals/*_retrieval.py graded the same way: capture cgr's CALLS edges for
the target, reduce each to (caller_file, callee_simple_name), and score that
set against a language oracle's edges. The languages differ only in which
file suffixes count, which relationship types count, whether a callee qn
carries an overload signature to strip, and whether the oracle publishes a
"covered" set of cleanly-parsed files to grade within. Those four axes are
this module's parameters; the language modules keep their own CLI, log lines,
and oracle wiring.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs

from . import constants as ec
from .cgr_graph import _capture
from .score import _prf
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

CallEdge = tuple[str, str]

EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)
CALLS = cs.RelationshipType.CALLS.value
INSTANTIATES = cs.RelationshipType.INSTANTIATES.value


def cgr_call_edges(
    target: Path,
    project: str,
    declared: frozenset[str],
    *,
    suffixes: str | tuple[str, ...],
    rel_types: tuple[str, ...] = (CALLS,),
    strip_signature: bool = False,
    covered: frozenset[str] | None = None,
) -> set[CallEdge]:
    """cgr's call edges for `target`, as (caller_file, callee_simple_name).

    `covered` restricts grading to the files the oracle parsed cleanly (its
    authoritative set); languages whose oracle covers everything pass None.
    """
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(suffixes)
    }
    edges: set[CallEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type not in rel_types:
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        if path is None or (covered is not None and path not in covered):
            continue
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1]
        if strip_signature:
            name = name.split(cs.CHAR_PAREN_OPEN)[0]
        if name in declared:
            edges.add((path, name))
    return edges


def score_retrieval(
    cgr: set[CallEdge],
    oracle: set[CallEdge],
    *,
    label: str,
    diff_prefix: str,
    edge_repr: str,
) -> ScoreResult:
    """One RETRIEVAL row plus its missing/extra diff bucket."""
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, label, cgr, oracle)
    if row is not None:
        rows.append(row)

        def _repr(edge: CallEdge) -> str:
            return edge_repr.format(file=edge[0], name=edge[1])

        diff[diff_prefix + label] = DiffBucket(
            missing=[_repr(e) for e in sorted(oracle - cgr)],
            extra=[_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=EMPTY_LOCATION, diff=diff)
