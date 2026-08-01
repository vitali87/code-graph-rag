# Methods of an impl whose target is NOT a locally declared type
# (`impl<P> TInput for Box<P>`) have no owner node cgr can attach to, so
# cgr anchors them to the module with DEFINES (the documented
# orphan-prevention fallback). The syn oracle emitted NO containment for
# them, so every such method graded as a false-positive edge on thrift
# (53 in src/protocol/mod.rs). The oracle must model the fallback.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from evals.cgr_graph import extract_cgr_rust_graph
from evals.oracles import run_rust_oracle, rust_available
from evals.score import score_edge_types

RS_SRC = """\
pub trait TInput {
    fn read_begin(&mut self) -> u32;
}

impl<P> TInput for Box<P>
where
    P: TInput + ?Sized,
{
    fn read_begin(&mut self) -> u32 {
        (**self).read_begin()
    }
}
"""


@pytest.mark.skipif(not rust_available(), reason="cargo toolchain not available")
def test_impl_on_external_type_methods_anchor_to_module(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text(RS_SRC)

    cgr = extract_cgr_rust_graph(tmp_path, tmp_path.name)
    oracle = run_rust_oracle(tmp_path)

    result = score_edge_types(
        cgr,
        oracle,
        (cs.RelationshipType.DEFINES, cs.RelationshipType.DEFINES_METHOD),
    )
    for row in result.rows:
        assert row["fp"] == 0, result.rows
        assert row["fn"] == 0, result.rows
