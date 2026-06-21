# (H) Covers Rust node SPAN (end_line) validation: cgr's end_line for each node is
# (H) graded against the syn oracle (which emits the whole-node span end), joined
# (H) on (kind, file, start) endpoints. Exercises doc comments, multi-line
# (H) attributes, a multi-line signature, a where-clause, and a multi-line closure
# (H) so the span is not trivially the start line.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_rust_graph
from evals.oracles import run_rust_oracle, rust_available
from evals.score import score_span

RS_SRC = """\
/// A documented struct
/// spanning several doc lines.
#[derive(Debug, Clone)]
pub struct Widget {
    name: String,
    size: u32,
}

impl Widget {
    pub fn area(
        &self,
        scale: u32,
    ) -> u32 {
        self.size * scale
    }
}

pub trait Drawable {
    fn draw(&self) -> String {
        String::from("x")
    }
}

pub fn standalone()
where
    u32: Sized,
{
    let cb = |v: u32| {
        v + 1
    };
    let _ = cb(2);
}
"""


def _require_rust() -> None:
    if not rust_available():
        pytest.skip("cargo toolchain not available")
    if cs.SupportedLanguage.RUST not in load_parsers()[0]:
        pytest.skip("rust parser not available")


def test_cgr_matches_syn_oracle_on_node_spans(tmp_path: Path) -> None:
    _require_rust()
    project = tmp_path / "rs_span"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_span"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(RS_SRC, encoding="utf-8")

    cgr = extract_cgr_rust_graph(project, project.name)
    oracle = run_rust_oracle(project)

    result = score_span(cgr, oracle, ec.RS_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    aggregate = by_label.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, (by_label, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    # (H) Guard the sample actually exercises multi-line spans (else it is vacuous).
    assert aggregate["tp"] >= 5, aggregate
