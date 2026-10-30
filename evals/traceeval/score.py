"""Edge-level precision/recall over TraceEval, computed the paper's way.

Micro-averaged within a language (TP/FP/FN summed over its programs, then
P/R/F1 once), macro-averaged across languages for the summary row, which is
how the paper's Table 3 reports its "Average" column. Three rows per
language:

- `<language>`: the paper protocol, the row that reads against Table 3;
- `<language>:static`: the static protocol (protocol.py);
- `<language>:exact`: the paper protocol against only the edges cgr bound
  `exact`, i.e. how precise the graph is where it claims certainty.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .. import constants as ec
from ..score import _prf
from ..types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow
from . import constants as tc
from .types_defs import ProgramResult, TraceEdge

_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)
_ProgramEdge = tuple[str, str, str]
_Pick = Callable[[ProgramResult], frozenset[TraceEdge]]


def _keyed(results: Iterable[ProgramResult], pick: _Pick) -> set[_ProgramEdge]:
    return {
        (result.program_id, caller, callee)
        for result in results
        for caller, callee in pick(result)
    }


def _repr(edge: _ProgramEdge) -> str:
    return tc.DIFF_EDGE_REPR.format(program=edge[0], caller=edge[1], callee=edge[2])


def _macro(rows: list[ScoreRow], label: str) -> ScoreRow:
    count = len(rows)
    return ScoreRow(
        category=tc.CATEGORY,
        label=label,
        tp=sum(r["tp"] for r in rows),
        fp=sum(r["fp"] for r in rows),
        fn=sum(r["fn"] for r in rows),
        precision=round(sum(r["precision"] for r in rows) / count, ec.ROUND_DIGITS),
        recall=round(sum(r["recall"] for r in rows) / count, ec.ROUND_DIGITS),
        f1=round(sum(r["f1"] for r in rows) / count, ec.ROUND_DIGITS),
    )


def _diff(truth: set[_ProgramEdge], predicted: set[_ProgramEdge]) -> DiffBucket:
    return DiffBucket(
        missing=[_repr(e) for e in sorted(truth - predicted)[: tc.DIFF_SAMPLE]],
        extra=[_repr(e) for e in sorted(predicted - truth)[: tc.DIFF_SAMPLE]],
    )


def score_results(results: list[ProgramResult]) -> ScoreResult:
    tables: dict[str, list[ScoreRow]] = {
        tc.PAPER_SUFFIX: [],
        tc.STATIC_SUFFIX: [],
        tc.EXACT_SUFFIX: [],
    }
    protocols: tuple[tuple[str, _Pick, _Pick], ...] = (
        (tc.PAPER_SUFFIX, lambda r: r.truth, lambda r: r.predicted),
        (tc.STATIC_SUFFIX, lambda r: r.truth_static, lambda r: r.predicted_static),
        (tc.EXACT_SUFFIX, lambda r: r.truth, lambda r: r.predicted_exact),
    )
    diff: dict[str, DiffBucket] = {}
    for language in tc.Language:
        chosen = [r for r in results if r.language == language]
        if not chosen:
            continue
        for suffix, pick_truth, pick_predicted in protocols:
            truth = _keyed(chosen, pick_truth)
            predicted = _keyed(chosen, pick_predicted)
            label = language.value + suffix
            if (row := _prf(tc.CATEGORY, label, predicted, truth)) is not None:
                tables[suffix].append(row)
            if suffix != tc.EXACT_SUFFIX:
                diff[label] = _diff(truth, predicted)
        if failed := [r for r in chosen if r.error]:
            diff[tc.FAILED_KEY.format(language=language.value)] = DiffBucket(
                missing=[
                    tc.FAILED_REPR.format(program=r.program_id, error=r.error)
                    for r in failed
                ],
                extra=[],
            )
    rows: list[ScoreRow] = []
    for suffix, table in tables.items():
        rows.extend(table)
        if suffix != tc.EXACT_SUFFIX and len(table) > 1:
            rows.append(_macro(table, tc.MACRO_LABEL + suffix))
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)
