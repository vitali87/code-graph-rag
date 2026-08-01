# Covers JavaScript node SPAN (end_line) validation: cgr's end_line is graded
# against the TS-compiler-API oracle over .js, joined on (kind, file, start).
# Exercises a class with a multi-line method signature, a multi-line arrow
# assigned to a const, and a nested arrow so spans are not trivially single line.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_js_graph
from evals.oracles import run_javascript_oracle, typescript_available
from evals.score import score_span

JS_SRC = """\
class Widget {
    area(
        scale,
    ) {
        return scale;
    }
}

function standalone() {
    const cb = (v) => {
        return v + 1;
    };
    return cb(2);
}

const arrow = (x) => {
    return x * 2;
};
"""


def _require_js() -> None:
    if not typescript_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.JS not in load_parsers()[0]:
        pytest.skip("javascript parser not available")


def test_cgr_matches_tsc_oracle_on_javascript_node_spans(tmp_path: Path) -> None:
    _require_js()
    project = tmp_path / "js_span_test"
    project.mkdir()
    (project / "main.js").write_text(JS_SRC, encoding="utf-8")

    cgr = extract_cgr_js_graph(project, project.name)
    oracle = run_javascript_oracle(project)

    result = score_span(cgr, oracle, ec.JS_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    aggregate = by_label.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, (by_label, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    assert aggregate["tp"] >= 4, aggregate
