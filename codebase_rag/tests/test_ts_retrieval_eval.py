from pathlib import Path

import pytest

from evals import constants as ec
from evals.oracles import typescript_available
from evals.ts_retrieval import (
    cgr_ts_call_edges,
    oracle_ts_call_edges,
    score_ts_retrieval,
)

needs_node = pytest.mark.skipif(
    not typescript_available(), reason="node toolchain not installed"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.ts").write_text(
        "export function free(): number { return 2; }\n"
        "export const dbl = (n: number): number => n * 2;\n",
        encoding="utf-8",
    )
    (root / "t.ts").write_text(
        "export class T {\n"
        "  helper(): number { return 1; }\n"
        "  caller(): number { return this.helper(); }\n"
        "  orphan(): number { return 9; }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "use.ts").write_text(
        'import { free, dbl } from "./util";\n'
        'import { T } from "./t";\n'
        "export function useIt(): number {\n"
        "  const t = new T();\n"
        "  return free() + dbl(3) + t.caller();\n"
        "}\n",
        encoding="utf-8",
    )


@needs_node
def test_oracle_captures_first_party_ts_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared = oracle_ts_call_edges(tmp_path)

    # (H) this.helper(), free(), dbl(), t.caller() are first-party calls.
    assert ("t.ts", "helper") in edges
    assert ("use.ts", "free") in edges
    assert ("use.ts", "dbl") in edges
    assert ("use.ts", "caller") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("t.ts", "orphan") not in edges
    assert {"helper", "caller", "orphan", "free", "dbl", "useIt"} <= declared


@needs_node
def test_cgr_matches_oracle_on_clean_ts_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared = oracle_ts_call_edges(tmp_path)
    cgr = cgr_ts_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


def test_score_ts_retrieval_prf() -> None:
    result = score_ts_retrieval(
        {("a.ts", "f"), ("a.ts", "g")}, {("a.ts", "f"), ("b.ts", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.TS_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
