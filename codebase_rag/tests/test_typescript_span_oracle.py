# Covers TypeScript node SPAN (end_line) validation: cgr's end_line for each
# node is graded against the TS-compiler-API oracle (which emits each node's
# full-span end line), joined on (kind, file, start). Exercises a class with a
# multi-line method signature, an interface, an enum, a type alias, a
# namespace, and a multi-line arrow function so spans are not trivially single
# line.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_ts_graph
from evals.oracles import run_typescript_oracle, typescript_available
from evals.score import score_span

TS_SRC = """\
export class Widget {
    area(
        scale: number,
    ): number {
        return scale;
    }
}

export interface Shape {
    area(): number;
}

export enum Color {
    Red,
    Green,
}

export type Pair = {
    a: number;
    b: number;
};

export namespace geo {
    export function dist(): number {
        return 1;
    }
}

export function standalone(): number {
    const cb = (v: number) => {
        return v + 1;
    };
    return cb(2);
}
"""


def _require_ts() -> None:
    if not typescript_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.TS not in load_parsers()[0]:
        pytest.skip("typescript parser not available")


def test_cgr_matches_tsc_oracle_on_node_spans(tmp_path: Path) -> None:
    _require_ts()
    project = tmp_path / "ts_span_test"
    project.mkdir()
    (project / "main.ts").write_text(TS_SRC, encoding="utf-8")

    cgr = extract_cgr_ts_graph(project, project.name)
    oracle = run_typescript_oracle(project)

    result = score_span(cgr, oracle, ec.TS_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    aggregate = by_label.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, (by_label, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    assert aggregate["tp"] >= 5, aggregate
