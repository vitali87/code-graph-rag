# Covers the JavaScript structure oracle harness (evals/oracles/ts_oracle run
# over .js/.jsx + evals/js_l1.py): the TS-compiler-API oracle is authoritative
# ground truth, and cgr's captured JavaScript nodes are graded against it on
# (kind, file, start_line).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_js_nodes
from evals.oracles import run_javascript_oracle, typescript_available
from evals.score import score_node_kinds
from evals.types_defs import GraphData

JS_SRC = """\
class Point {
    constructor(x) { this.x = x; }
    area() { return this.x; }
}

function freeFn(a) { return a + 1; }
const arrow = (b) => b * 2;
const obj = { method() { return 1; } };
[1, 2].forEach((n) => freeFn(n));
"""


def _require_js() -> None:
    if not typescript_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.JS not in load_parsers()[0]:
        pytest.skip("javascript parser not available")


def test_cgr_matches_tsc_oracle_on_javascript_structure(tmp_path: Path) -> None:
    _require_js()
    project = tmp_path / "js_oracle_test"
    project.mkdir()
    (project / "app.js").write_text(JS_SRC, encoding="utf-8")

    cgr = GraphData(
        nodes=extract_cgr_js_nodes(project, project.name),
        edges=set(),
        name_edges=set(),
    )
    oracle = run_javascript_oracle(project)

    result = score_node_kinds(cgr, oracle, ec.JS_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("Class", "Function", "Method"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)
