# (H) Covers the Rust structure oracle harness (evals/oracles/rs_oracle +
# (H) evals/rust_l1.py): the syn-based oracle is authoritative ground truth, and
# (H) cgr's captured Rust nodes are graded against it on (kind, file, start_line).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_rust_nodes
from evals.oracles import run_rust_oracle, rust_available
from evals.score import score_node_kinds
from evals.types_defs import GraphData

RS_SRC = """\
pub struct Point { pub x: i32, pub y: i32 }
pub enum Direction { North, South }
pub trait Shape { fn area(&self) -> f64; }
pub type Meters = f64;

pub fn free_fn(a: i32) -> i32 { a + 1 }

impl Point {
    pub fn new(x: i32, y: i32) -> Self { Point { x, y } }
}

impl Shape for Point {
    fn area(&self) -> f64 { 0.0 }
}
"""


def _require_rust() -> None:
    if not rust_available():
        pytest.skip("cargo toolchain not available")
    if cs.SupportedLanguage.RUST not in load_parsers()[0]:
        pytest.skip("rust parser not available")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "rs_oracle_test"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_oracle_test"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(RS_SRC, encoding="utf-8")
    return project


def test_cgr_matches_syn_oracle_on_rust_structure(tmp_path: Path) -> None:
    _require_rust()
    project = _project(tmp_path)
    cgr = GraphData(
        nodes=extract_cgr_rust_nodes(project, project.name),
        edges=set(),
        name_edges=set(),
    )
    oracle = GraphData(nodes=run_rust_oracle(project), edges=set(), name_edges=set())

    result = score_node_kinds(cgr, oracle, ec.RS_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("Class", "Interface", "Enum", "Type", "Function", "Method"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)
