from pathlib import Path

import pytest

from evals import constants as ec
from evals.c_retrieval import (
    cgr_c_call_edges,
    oracle_c_call_edges,
    score_c_retrieval,
)
from evals.oracles import cpp_available

needs_clang = pytest.mark.skipif(
    not cpp_available(), reason="libclang (clang.cindex) not importable"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "util.h").write_text(
        "int add(int a, int b);\nint mul(int a, int b);\nint orphan(void);\n",
        encoding="utf-8",
    )
    (root / "util.c").write_text(
        '#include "util.h"\n'
        "int add(int a, int b) { return a + b; }\n"
        "int mul(int a, int b) { return a * b; }\n"
        "int orphan(void) { return 9; }\n",
        encoding="utf-8",
    )
    # (H) No system #includes: the fixture parses cleanly regardless of whether an
    # (H) SDK sysroot is discoverable, so coverage is deterministic in any CI.
    (root / "main.c").write_text(
        '#include "util.h"\n'
        "static int compute(int x, int y) { return add(x, y) + mul(x, y); }\n"
        "int main(void) { return compute(2, 3); }\n",
        encoding="utf-8",
    )


@needs_clang
def test_oracle_captures_first_party_c_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared, covered = oracle_c_call_edges(tmp_path)

    # (H) add(), mul() (in compute), compute() (in main) are first-party calls.
    assert ("main.c", "add") in edges
    assert ("main.c", "mul") in edges
    assert ("main.c", "compute") in edges
    # (H) orphan is defined but never called -> never a call edge.
    assert ("util.c", "orphan") not in edges
    assert {"add", "mul", "compute", "main", "orphan"} <= declared
    # (H) Both header-free sources parse cleanly, so both are graded.
    assert {"main.c", "util.c"} <= covered


@needs_clang
def test_cgr_matches_oracle_on_clean_c_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared, covered = oracle_c_call_edges(tmp_path)
    cgr = cgr_c_call_edges(tmp_path, tmp_path.name, declared, covered)
    assert cgr == oracle


def test_score_c_retrieval_prf() -> None:
    result = score_c_retrieval(
        {("a.c", "f"), ("a.c", "g")}, {("a.c", "f"), ("b.c", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.C_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
