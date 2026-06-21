# (H) Covers Go containment-edge validation: cgr's DEFINES (Module->top-level
# (H) func/type) and DEFINES_METHOD (struct Class->receiver method) edges are
# (H) graded against the independent go/ast oracle (evals/oracles/go_ast.go),
# (H) joined on (kind, file, line) endpoints. The sample exercises a same-file
# (H) method and a cross-file method (receiver type declared in another file).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_go_graph
from evals.oracles import go_available, run_go_oracle
from evals.score import score_edge_types

GO_TYPES = """\
package demo

type Shape interface { Area() float64 }

type Point struct{ X int }

func (p Point) Area() float64 { return 1.0 }
"""

GO_MORE = """\
package demo

func Free(a int) int { return a + 1 }

func (p Point) Scale(k int) int { return p.X * k }
"""


def _require_go() -> None:
    if not go_available():
        pytest.skip("go toolchain not available")
    if cs.SupportedLanguage.GO not in load_parsers()[0]:
        pytest.skip("go parser not available")


def test_cgr_matches_go_oracle_on_containment_edges(tmp_path: Path) -> None:
    _require_go()
    project = tmp_path / "go_edge_test"
    project.mkdir()
    (project / "types.go").write_text(GO_TYPES, encoding="utf-8")
    (project / "more.go").write_text(GO_MORE, encoding="utf-8")

    cgr = extract_cgr_go_graph(project, project.name)
    oracle = run_go_oracle(project)

    result = score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in (
        cs.RelationshipType.DEFINES.value,
        cs.RelationshipType.DEFINES_METHOD.value,
    ):
        row = by_label.get(label)
        assert row is not None, (label, by_label, result.diff)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (
            label,
            row,
            result.diff,
        )
