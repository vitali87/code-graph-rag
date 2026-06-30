from pathlib import Path

import pytest

from evals import constants as ec
from evals.cpp_retrieval import (
    cgr_cpp_call_edges,
    oracle_cpp_call_edges,
    score_cpp_retrieval,
)
from evals.oracles import cpp_available

needs_clang = pytest.mark.skipif(
    not cpp_available(), reason="libclang (clang.cindex) not importable"
)


def _make_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # (H) No #includes: the fixture parses cleanly regardless of whether an SDK
    # (H) libc++ is discoverable, so coverage is deterministic in any CI. All decls
    # (H) live inside a namespace, exercising the namespaced caller-qn path (free
    # (H) functions and an inline method) that the libclang oracle grades cgr against.
    (root / "lib.cc").write_text(
        "namespace demo {\n"
        "int add(int a, int b) { return a + b; }\n"
        "int mul(int a, int b) { return a * b; }\n"
        "int orphan(int a) { return a; }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "main.cc").write_text(
        "namespace demo {\n"
        "int add(int a, int b);\n"
        "int mul(int a, int b);\n"
        "int compute(int x) { return add(x, x) + mul(x, x); }\n"
        "class Runner {\n"
        " public:\n"
        "  int run(int x) { return compute(x); }\n"
        "};\n"
        "}\n",
        encoding="utf-8",
    )


@needs_clang
def test_oracle_captures_first_party_cpp_calls(tmp_path: Path) -> None:
    _make_project(tmp_path)
    edges, declared, covered = oracle_cpp_call_edges(tmp_path)

    # (H) add(), mul() (in compute), compute() (in Runner::run) are first-party.
    assert ("main.cc", "add") in edges
    assert ("main.cc", "mul") in edges
    assert ("main.cc", "compute") in edges
    # (H) orphan is defined but never called -> never a call edge.
    assert ("lib.cc", "orphan") not in edges
    assert {"add", "mul", "compute", "run", "orphan"} <= declared
    # (H) Both header-free sources parse cleanly, so both are graded.
    assert {"main.cc", "lib.cc"} <= covered


@needs_clang
def test_cgr_matches_oracle_on_clean_cpp_project(tmp_path: Path) -> None:
    _make_project(tmp_path)
    oracle, declared, covered = oracle_cpp_call_edges(tmp_path)
    cgr = cgr_cpp_call_edges(tmp_path, tmp_path.name, declared, covered)
    assert cgr == oracle


def test_score_cpp_retrieval_prf() -> None:
    result = score_cpp_retrieval(
        {("a.cc", "f"), ("a.cc", "g")}, {("a.cc", "f"), ("b.cc", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.CPP_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
