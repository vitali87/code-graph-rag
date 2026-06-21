# (H) Covers Go node SPAN (end_line) validation: cgr's end_line for each node is
# (H) graded against the go/ast oracle (which emits each declaration's last-token
# (H) line), joined on (kind, file, start). Exercises a multi-line struct, a
# (H) grouped `type (...)` block, an interface, and a multi-line method body.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_go_graph
from evals.oracles import go_available, run_go_oracle
from evals.score import score_span

GO_SRC = """\
package demo

type Shape interface {
	Area() float64
	Name() string
}

type Point struct {
	X int
	Y int
}

type (
	Meters int
	Label  string
)

func (p Point) Area(
	scale float64,
) float64 {
	return float64(p.X) * scale
}

func Free(a int) int {
	return a + 1
}
"""


def _require_go() -> None:
    if not go_available():
        pytest.skip("go toolchain not available")
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")


def test_cgr_matches_go_oracle_on_node_spans(tmp_path: Path) -> None:
    _require_go()
    project = tmp_path / "go_span_test"
    project.mkdir()
    (project / "demo.go").write_text(GO_SRC, encoding="utf-8")

    cgr = extract_cgr_go_graph(project, project.name)
    oracle = run_go_oracle(project)

    result = score_span(cgr, oracle, ec.GO_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    aggregate = by_label.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, (by_label, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    assert aggregate["tp"] >= 5, aggregate
