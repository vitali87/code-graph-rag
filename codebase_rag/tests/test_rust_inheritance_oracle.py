# Covers Rust inheritance-edge validation: cgr's INHERITS (supertrait bound)
# and IMPLEMENTS (`impl Trait for Type`) edges graded against the syn oracle
# by (source node, base simple name).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_rust_graph
from evals.oracles import run_rust_oracle, rust_available
from evals.score import score_name_edge_types

RS_SRC = """\
pub trait Shape {}
pub trait Drawable: Shape {}

pub struct Circle;

impl Shape for Circle {}
impl Drawable for Circle {}
"""


def _require_rust() -> None:
    if not rust_available():
        pytest.skip("cargo toolchain not available")
    if cs.SupportedLanguage.RUST not in load_parsers()[0]:
        pytest.skip("rust parser not available")


def test_cgr_matches_syn_oracle_on_inheritance_edges(tmp_path: Path) -> None:
    _require_rust()
    project = tmp_path / "rs_inh_edge"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_inh_edge"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(RS_SRC, encoding="utf-8")

    cgr = extract_cgr_rust_graph(project, project.name)
    oracle = run_rust_oracle(project)

    result = score_name_edge_types(cgr, oracle, ec.INHERITANCE_NAME_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in (
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.IMPLEMENTS.value,
    ):
        row = by_label.get(label)
        assert row is not None, (label, by_label, result.diff)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (
            label,
            row,
            result.diff,
        )
