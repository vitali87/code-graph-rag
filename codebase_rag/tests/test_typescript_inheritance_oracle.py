# Covers TypeScript inheritance-edge validation: cgr's INHERITS (class &
# interface extends) and IMPLEMENTS (class implements) edges are graded
# against the TypeScript-compiler-API oracle, by (source node, base name).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_ts_graph
from evals.oracles import run_typescript_oracle, typescript_available
from evals.score import score_name_edge_types

TS_SRC = """\
export interface Shape {}
export interface Drawable {}
export interface Big extends Shape, Drawable {}
export class Base {}
export class Circle extends Base implements Shape, Drawable {}
"""


def _require_ts() -> None:
    if not typescript_available():
        pytest.skip("node/npm toolchain not available")
    if cs.SupportedLanguage.TS not in load_parsers()[0]:
        pytest.skip("typescript parser not available")


def test_cgr_matches_tsc_oracle_on_inheritance_edges(tmp_path: Path) -> None:
    _require_ts()
    project = tmp_path / "ts_inh_edge"
    project.mkdir()
    (project / "lib.ts").write_text(TS_SRC, encoding="utf-8")

    cgr = extract_cgr_ts_graph(project, project.name)
    oracle = run_typescript_oracle(project)

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
