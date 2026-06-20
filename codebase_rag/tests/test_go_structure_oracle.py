# (H) Covers the Go structure oracle harness (evals/oracles/go_ast.go +
# (H) evals/go_l1.py): the go/ast oracle is authoritative ground truth, and cgr's
# (H) captured Go nodes are graded against it on (kind, file, start_line).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import extract_cgr_go_nodes
from evals.oracles import go_available, run_go_oracle
from evals.score import score_node_kinds
from evals.types_defs import GraphData

GO_SRC = """package shapes

type Point struct {
\tX int
\tY int
}

type Shape interface {
\tArea() float64
}

type Celsius float64

func NewPoint(x int, y int) Point {
\treturn Point{X: x, Y: y}
}

func (p Point) Area() float64 {
\treturn 0.0
}
"""


def _require_go() -> None:
    if not go_available():
        pytest.skip("go toolchain not available")
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")


def _go_project(tmp_path: Path) -> Path:
    project = tmp_path / "shapes_mod"
    project.mkdir()
    (project / "go.mod").write_text("module shapes_mod\n\ngo 1.22\n", encoding="utf-8")
    (project / "shapes.go").write_text(GO_SRC, encoding="utf-8")
    return project


def _names(nodes: dict, kind: cs.NodeLabel) -> set[str]:
    return {node.name for key, node in nodes.items() if key.kind == kind.value}


def test_oracle_labels_go_declarations(tmp_path: Path) -> None:
    _require_go()
    oracle = run_go_oracle(_go_project(tmp_path))
    assert _names(oracle, cs.NodeLabel.CLASS) == {"Point"}
    assert _names(oracle, cs.NodeLabel.INTERFACE) == {"Shape"}
    assert _names(oracle, cs.NodeLabel.TYPE) == {"Celsius"}
    assert _names(oracle, cs.NodeLabel.FUNCTION) == {"NewPoint"}
    # (H) go/ast knows Area has a receiver, so it is a Method, not a Function.
    assert _names(oracle, cs.NodeLabel.METHOD) == {"Area"}


def test_cgr_matches_oracle_on_type_declarations(tmp_path: Path) -> None:
    _require_go()
    project = _go_project(tmp_path)
    cgr = GraphData(
        nodes=extract_cgr_go_nodes(project, project.name), edges=set(), name_edges=set()
    )
    oracle = GraphData(nodes=run_go_oracle(project), edges=set(), name_edges=set())

    result = score_node_kinds(
        cgr,
        oracle,
        (cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE, cs.NodeLabel.TYPE),
    )
    by_label = {row["label"]: row for row in result.rows}
    for label in (
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.INTERFACE.value,
        cs.NodeLabel.TYPE.value,
    ):
        assert by_label[label]["recall"] == 1.0, (label, by_label[label])
        assert by_label[label]["precision"] == 1.0, (label, by_label[label])
