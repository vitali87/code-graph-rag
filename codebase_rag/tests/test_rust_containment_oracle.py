# Covers Rust containment-edge validation: cgr's DEFINES (module to item or
# nested module) and DEFINES_METHOD (struct/trait to method) edges are graded
# against the independent syn oracle (evals/oracles/rs_oracle), joined on
# (kind, file, line) endpoints. Exercises an inherent impl, a trait method,
# and an impl inside a nested `mod`.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_rust_graph
from evals.oracles import run_rust_oracle, rust_available
from evals.score import score_edge_types

RS_SRC = """\
pub trait Shape {
    fn area(&self) -> f64 { 0.0 }
}

pub struct Point {
    x: i32,
}

impl Point {
    pub fn new() -> Point {
        Point { x: 0 }
    }
}

impl Shape for Point {
    fn area(&self) -> f64 {
        1.0
    }
}

pub fn free() -> i32 {
    1
}

pub mod inner {
    pub struct Widget {
        w: i32,
    }

    impl Widget {
        pub fn build(&self) -> i32 {
            self.w
        }
    }
}
"""


def _require_rust() -> None:
    if not rust_available():
        pytest.skip("cargo toolchain not available")
    if cs.SupportedLanguage.RUST not in load_parsers()[0]:
        pytest.skip("rust parser not available")


def test_cgr_matches_syn_oracle_on_containment_edges(tmp_path: Path) -> None:
    _require_rust()
    project = tmp_path / "rs_edge"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_edge"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(RS_SRC, encoding="utf-8")

    cgr = extract_cgr_rust_graph(project, project.name)
    oracle = run_rust_oracle(project)

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
