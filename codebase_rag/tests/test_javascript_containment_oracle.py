# Covers JavaScript containment-edge validation: cgr's DEFINES (file module
# -> class / top-level function) and DEFINES_METHOD (class -> method) edges
# are graded against the TypeScript-compiler-API oracle run over .js, joined
# on (kind, file, line).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_js_graph
from evals.oracles import run_javascript_oracle, typescript_available
from evals.score import score_edge_types

JS_SRC = """\
export class Point {
    constructor() { this.x = 0; }
    area() { return 1.0; }
}

export function free() { return 1; }
"""

# `View.prototype.lookup = function () {...}` is DEFINEd by the CONSTRUCTOR
# node in cgr's model (the prototype pass registers `module.View.lookup`
# under the constructor). The oracle used to expect a module parent, which
# only matched cgr's since-cured anonymous-twin duplicate; it must model
# the constructor containment, including a constructor declared as a
# var-assigned function expression and an assignment nested in a function.
JS_PROTOTYPE_SRC = """\
function View(name) {
    this.name = name;
}

View.prototype.lookup = function (key) {
    return this.name + key;
};

var Store = function (items) {
    this.items = items;
};

Store.prototype.get = function (i) {
    return this.items[i];
};

function install() {
    View.prototype.reset = function () {
        this.name = "";
    };
}

var Conn = (exports.Conn = function (stream) {
    this.stream = stream;
});

Conn.prototype.close = function () {
    this.stream = null;
};

function wrap(base) {
    function Inner(x) {
        base.call(this, x);
    }

    Inner.prototype.run = function () {
        return this.x;
    };

    return Inner;
}
"""


def _require_js() -> None:
    if not typescript_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.JS not in load_parsers()[0]:
        pytest.skip("javascript parser not available")


def test_cgr_matches_tsc_oracle_on_js_containment_edges(tmp_path: Path) -> None:
    _require_js()
    project = tmp_path / "js_edge"
    project.mkdir()
    (project / "lib.js").write_text(JS_SRC, encoding="utf-8")

    cgr = extract_cgr_js_graph(project, project.name)
    oracle = run_javascript_oracle(project)

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


def test_cgr_matches_tsc_oracle_on_prototype_method_containment(
    tmp_path: Path,
) -> None:
    _require_js()
    project = tmp_path / "js_proto"
    project.mkdir()
    (project / "view.js").write_text(JS_PROTOTYPE_SRC, encoding="utf-8")

    cgr = extract_cgr_js_graph(project, project.name)
    oracle = run_javascript_oracle(project)

    result = score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    row = by_label.get(cs.RelationshipType.DEFINES.value)
    assert row is not None, (by_label, result.diff)
    assert row["precision"] == 1.0 and row["recall"] == 1.0, (row, result.diff)
