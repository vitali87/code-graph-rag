from pathlib import Path

import pytest

from evals import constants as ec
from evals.go_retrieval import (
    cgr_go_call_edges,
    oracle_go_call_edges,
    score_go_retrieval,
)
from evals.oracles import go_available

needs_go = pytest.mark.skipif(not go_available(), reason="go toolchain not installed")


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.go").write_text(
        "package main\n\n"
        "func helper() int { return 1 }\n\n"
        "func run() int { return helper() }\n\n"
        "func orphan() int { return 2 }\n\n"
        "func main() { run() }\n",
        encoding="utf-8",
    )


@needs_go
def test_oracle_captures_first_party_go_calls(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    edges, declared = oracle_go_call_edges(tmp_path)

    assert ("main.go", "helper") in edges
    assert ("main.go", "run") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("main.go", "orphan") not in edges
    assert {"helper", "run", "orphan", "main"} <= declared


@needs_go
def test_cgr_matches_oracle_on_clean_go_repo(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    oracle, declared = oracle_go_call_edges(tmp_path)
    cgr = cgr_go_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


def test_score_go_retrieval_prf() -> None:
    result = score_go_retrieval(
        {("a.go", "f"), ("a.go", "g")}, {("a.go", "f"), ("b.go", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.GO_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
