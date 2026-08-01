# Covers the L1 eval span grading (evals/score.score_span): among nodes both
# cgr and the oracle identify by (kind, file, start), it grades how often cgr's
# end_line agrees with the oracle's. A disagreement must surface as fp+fn (not
# be masked by node identity already being 1.0), and nodes only one side has
# must not be graded at all.
from __future__ import annotations

from codebase_rag import constants as cs
from evals import constants as ec
from evals.score import score_span
from evals.types_defs import DefNode, GraphData, NodeKey

_FUNC = cs.NodeLabel.FUNCTION.value
_KINDS = (cs.NodeLabel.FUNCTION,)


def _graph(*nodes: tuple[str, int, int]) -> GraphData:
    # Each node is (file, start, end) for a Function.
    mapping: dict[NodeKey, DefNode] = {}
    for file, start, end in nodes:
        key = NodeKey(_FUNC, file, start)
        mapping[key] = DefNode(key, "f", end)
    return GraphData(nodes=mapping, edges=set(), name_edges=set())


def test_span_exact_match_scores_perfect() -> None:
    cgr = _graph(("a.rs", 1, 5), ("a.rs", 10, 20))
    oracle = _graph(("a.rs", 1, 5), ("a.rs", 10, 20))
    by_label = {row["label"]: row for row in score_span(cgr, oracle, _KINDS).rows}
    row = by_label[_FUNC]
    assert row["precision"] == 1.0 and row["recall"] == 1.0
    assert row["tp"] == 2 and row["fp"] == 0 and row["fn"] == 0


def test_span_end_line_mismatch_is_penalized_and_surfaced() -> None:
    cgr = _graph(("a.rs", 1, 5), ("a.rs", 10, 99))
    oracle = _graph(("a.rs", 1, 5), ("a.rs", 10, 20))
    result = score_span(cgr, oracle, _KINDS)
    by_label = {row["label"]: row for row in result.rows}
    row = by_label[_FUNC]
    assert row["tp"] == 1 and row["fp"] == 1 and row["fn"] == 1
    assert row["precision"] == 0.5 and row["recall"] == 0.5
    bucket = result.diff[ec.DIFF_SPAN_PREFIX + _FUNC]
    assert any("10-20" in line for line in bucket["missing"]), bucket
    assert any("10-99" in line for line in bucket["extra"]), bucket


def test_span_only_grades_co_identified_nodes() -> None:
    # cgr has an extra node (start 30) the oracle lacks; it must not be graded.
    cgr = _graph(("a.rs", 1, 5), ("a.rs", 30, 40))
    oracle = _graph(("a.rs", 1, 5))
    by_label = {row["label"]: row for row in score_span(cgr, oracle, _KINDS).rows}
    row = by_label[_FUNC]
    assert row["tp"] == 1 and row["fp"] == 0 and row["fn"] == 0
