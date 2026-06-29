from pathlib import Path

import pytest

from evals import constants as ec
from evals.oracles import rust_available
from evals.rust_retrieval import (
    cgr_rust_call_edges,
    oracle_rust_call_edges,
    score_rust_retrieval,
)

needs_rust = pytest.mark.skipif(
    not rust_available(), reason="rust toolchain not installed"
)


def _make_crate(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lib.rs").write_text(
        "pub struct T;\n\n"
        "impl T {\n"
        "    pub fn helper(&self) -> i32 { 1 }\n"
        "    pub fn caller(&self) -> i32 { self.helper() }\n"
        "    pub fn make() -> T { T }\n"
        "    pub fn orphan(&self) -> i32 { 9 }\n"
        "}\n\n"
        "pub fn free() -> i32 { 2 }\n\n"
        "pub fn use_it() -> i32 {\n"
        "    let t = T::make();\n"
        "    free() + t.caller()\n"
        "}\n",
        encoding="utf-8",
    )


@needs_rust
def test_oracle_captures_first_party_rust_calls(tmp_path: Path) -> None:
    _make_crate(tmp_path)
    edges, declared = oracle_rust_call_edges(tmp_path)

    # (H) self.helper(), T::make(), free(), t.caller() are all first-party calls.
    assert ("lib.rs", "helper") in edges
    assert ("lib.rs", "make") in edges
    assert ("lib.rs", "free") in edges
    assert ("lib.rs", "caller") in edges
    # (H) orphan is declared but never called -> never a call edge.
    assert ("lib.rs", "orphan") not in edges
    assert {"helper", "caller", "make", "free", "use_it", "orphan"} <= declared


@needs_rust
def test_cgr_matches_oracle_on_clean_rust_crate(tmp_path: Path) -> None:
    _make_crate(tmp_path)
    oracle, declared = oracle_rust_call_edges(tmp_path)
    cgr = cgr_rust_call_edges(tmp_path, tmp_path.name, declared)
    assert cgr == oracle


def test_score_rust_retrieval_prf() -> None:
    result = score_rust_retrieval(
        {("a.rs", "f"), ("a.rs", "g")}, {("a.rs", "f"), ("b.rs", "h")}
    )
    row = next(r for r in result.rows if r["label"] == ec.RUST_RETRIEVAL_LABEL)
    assert (row["tp"], row["fp"], row["fn"]) == (1, 1, 1)
