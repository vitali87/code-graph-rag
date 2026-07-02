from pathlib import Path

import pytest

from evals import constants as ec
from evals.js_retrieval import (
    cgr_js_call_edges,
    oracle_js_call_edges,
    score_js_retrieval,
)
from evals.oracles import typescript_available

needs_node = pytest.mark.skipif(
    not typescript_available(), reason="node toolchain not installed"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.js").write_text(
        "export function free() { return 2; }\nexport const dbl = (n) => n * 2;\n",
        encoding="utf-8",
    )
    (root / "t.js").write_text(
        "export class T {\n"
        "  helper() { return 1; }\n"
        "  caller() { return this.helper(); }\n"
        "  orphan() { return 9; }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "use.js").write_text(
        'import { free, dbl } from "./util";\n'
        'import { T } from "./t";\n'
        "export function useIt() {\n"
        "  const t = new T();\n"
        "  return free() + dbl(3) + t.caller();\n"
        "}\n",
        encoding="utf-8",
    )


@needs_node
def test_oracle_captures_first_party_js_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared = oracle_js_call_edges(tmp_path)

    assert ("t.js", "helper") in edges
    assert ("use.js", "free") in edges
    assert ("use.js", "dbl") in edges
    assert ("use.js", "caller") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("t.js", "orphan") not in edges
    assert {"helper", "caller", "orphan", "free", "dbl", "useIt"} <= declared


@needs_node
def test_cgr_matches_oracle_on_clean_js_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared = oracle_js_call_edges(tmp_path)
    cgr = cgr_js_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


def test_score_js_retrieval_prf() -> None:
    result = score_js_retrieval(
        {("a.js", "f"), ("a.js", "g")}, {("a.js", "f"), ("b.js", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.JS_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
