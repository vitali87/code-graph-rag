# (H) Covers the TypeScript structure oracle harness (evals/oracles/ts_oracle +
# (H) evals/ts_l1.py): the TS-compiler-API oracle is authoritative ground truth,
# (H) and cgr's captured TypeScript nodes are graded against it on
# (H) (kind, file, start_line).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_ts_nodes
from evals.oracles import run_typescript_oracle, typescript_available
from evals.score import score_node_kinds
from evals.types_defs import GraphData

TS_SRC = """\
export interface Shape { area(): number; }
export type Meters = number;
export enum Color { Red, Green, Blue }

export class Point implements Shape {
    x: number;
    constructor(x: number) { this.x = x; }
    area(): number { return this.x; }
}

export function freeFn(a: number): number { return a + 1; }
export const arrow = (b: number): number => b * 2;
[1, 2].forEach((n) => freeFn(n));
"""


def _require_ts() -> None:
    if not typescript_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.TS not in load_parsers()[0]:
        pytest.skip("typescript parser not available")


def test_cgr_matches_tsc_oracle_on_typescript_structure(tmp_path: Path) -> None:
    _require_ts()
    project = tmp_path / "ts_oracle_test"
    project.mkdir()
    (project / "app.ts").write_text(TS_SRC, encoding="utf-8")

    cgr = GraphData(
        nodes=extract_cgr_ts_nodes(project, project.name),
        edges=set(),
        name_edges=set(),
    )
    oracle = run_typescript_oracle(project)

    result = score_node_kinds(cgr, oracle, ec.TS_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("Class", "Interface", "Enum", "Type", "Function", "Method"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)
