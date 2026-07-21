# Covers Rust closure containment: a closure is DEFINEd by its nearest
# enclosing function-like scope (impl/trait method -> Method, free fn or outer
# closure -> Function). cgr routes closures through its free-function path; the
# syn oracle (evals/oracles/rs_oracle) emits the matching DEFINES via a stack
# of enclosing function-likes. Joined on (kind, file, line) endpoints.
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
pub struct Foo;

impl Foo {
    pub fn run(&self) -> i32 {
        let c = |x: i32| x + 1;
        let nested = || {
            let inner = |z: i32| z * 2;
            inner(5)
        };
        c(2) + nested()
    }
}

pub trait Bar {
    fn act(&self) -> i32 {
        let t = |q: i32| q - 1;
        t(9)
    }
}

pub fn free() -> i32 {
    let d = |y: i32| y + 2;
    d(3)
}
"""


def _require_rust() -> None:
    if not rust_available():
        pytest.skip("cargo toolchain not available")
    if cs.SupportedLanguage.RUST not in load_parsers()[0]:
        pytest.skip("rust parser not available")


def test_cgr_matches_syn_oracle_on_closure_containment(tmp_path: Path) -> None:
    _require_rust()
    project = tmp_path / "rs_clo_edge"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_clo_edge"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(RS_SRC, encoding="utf-8")

    cgr = extract_cgr_rust_graph(project, project.name)
    oracle = run_rust_oracle(project)

    result = score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    row = by_label.get(cs.RelationshipType.DEFINES.value)
    assert row is not None, (by_label, result.diff)
    assert row["precision"] == 1.0 and row["recall"] == 1.0, (row, result.diff)
    # The method-nested closures must contribute resolvable DEFINES edges,
    # not just the free-function one (the gap this fix closes).
    assert row["tp"] >= 5, (row, result.diff)
