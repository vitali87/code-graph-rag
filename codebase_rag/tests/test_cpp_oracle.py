# (H) Covers the C++ structure oracle (evals/oracles/cpp_oracle.py): a libclang
# (H) oracle driven by a compile_commands.json resolves #includes and expands
# (H) macros to the true translation-unit AST, which tree-sitter cannot do. cgr's
# (H) C++ nodes, containment edges, and spans are graded against it on
# (H) (kind, file, start_line). The sample exercises a header-declared class
# (H) (resolved via an -I include path), a macro-typed method, out-of-class method
# (H) definitions, a constructor, an inline method, a struct, and a free function.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_cpp_graph
from evals.oracles import cpp_available, run_cpp_oracle
from evals.score import score_edge_types, score_node_kinds, score_span
from evals.types_defs import ScoreRow

SHAPE_H = """\
#pragma once
#define AREA_T double

struct Point {
    int x;
    int y;
};

class Shape {
public:
    Shape(int id);
    AREA_T area() const;
    void scale(
        double factor
    );
    int inline_id() const { return id_; }
private:
    int id_;
};
"""

SHAPE_CPP = """\
#include "shape.h"

Shape::Shape(int id) : id_(id) {
}

AREA_T Shape::area() const {
    return 1.0;
}

void Shape::scale(double factor) {
    id_ = static_cast<int>(factor);
}

int helper(int n) {
    return n * 2;
}
"""


def _require_cpp() -> None:
    if not cpp_available():
        pytest.skip("libclang not available")
    if cs.SupportedLanguage.CPP not in load_parsers()[0]:
        pytest.skip("cpp parser not available")


def _aggregate(rows: list[ScoreRow]) -> ScoreRow | None:
    return next((r for r in rows if r["label"] == ec.AGGREGATE_LABEL), None)


def test_cgr_matches_libclang_oracle_on_cpp_structure(tmp_path: Path) -> None:
    _require_cpp()
    project = tmp_path / "cpp_proj"
    (project / "include").mkdir(parents=True)
    (project / "src").mkdir(parents=True)
    (project / "include" / "shape.h").write_text(SHAPE_H, encoding="utf-8")
    (project / "src" / "shape.cpp").write_text(SHAPE_CPP, encoding="utf-8")

    src = (project / "src" / "shape.cpp").resolve()
    include = (project / "include").resolve()
    compdb = [
        {
            "directory": str(project.resolve()),
            "file": str(src),
            "command": f"clang++ -std=c++17 -I{include} -c {src}",
        }
    ]
    (project / ec.CPP_COMPDB_FILENAME).write_text(json.dumps(compdb), encoding="utf-8")

    cgr = extract_cgr_cpp_graph(project, project.name)
    oracle = run_cpp_oracle(project)

    for label, result in (
        ("nodes", score_node_kinds(cgr, oracle, ec.CPP_SCORED_NODE_KINDS)),
        ("edges", score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)),
        ("spans", score_span(cgr, oracle, ec.CPP_SCORED_NODE_KINDS)),
    ):
        aggregate = _aggregate(result.rows)
        assert aggregate is not None, (label, result.rows, result.diff)
        assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
            label,
            aggregate,
            result.diff,
        )
    # (H) Guard the sample is non-trivial (class + struct + 4 methods + function).
    node_aggregate = _aggregate(
        score_node_kinds(cgr, oracle, ec.CPP_SCORED_NODE_KINDS).rows
    )
    assert node_aggregate is not None and node_aggregate["tp"] >= 7, node_aggregate
