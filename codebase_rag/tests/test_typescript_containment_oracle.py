# Covers TypeScript containment-edge validation: cgr's DEFINES (file module
# -> every named type, even nested) and DEFINES_METHOD (class/namespace ->
# method) edges are graded against the independent TypeScript-compiler-API
# oracle, joined on (kind, file, line). Exercises a class method, a top-level
# function, and a namespace (class + function as methods of the namespace).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_ts_graph
from evals.oracles import run_typescript_oracle, typescript_available
from evals.score import score_edge_types

TS_SRC = """\
export interface Shape { area(): number; }

export enum Color { Red, Green }

export class Point implements Shape {
    x: number = 0;
    area(): number { return 1.0; }
}

export function free(): number { return 1; }

export namespace geo {
    export class Widget { build(): number { return 1; } }
    export function helper(): number { return 2; }
}
"""


def _require_ts() -> None:
    if not typescript_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.TS not in load_parsers()[0]:
        pytest.skip("typescript parser not available")


def test_cgr_matches_tsc_oracle_on_containment_edges(tmp_path: Path) -> None:
    _require_ts()
    project = tmp_path / "ts_edge"
    project.mkdir()
    (project / "lib.ts").write_text(TS_SRC, encoding="utf-8")

    cgr = extract_cgr_ts_graph(project, project.name)
    oracle = run_typescript_oracle(project)

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
