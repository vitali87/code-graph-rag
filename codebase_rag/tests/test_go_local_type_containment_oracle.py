# (H) A function-local Go type (`type alias T` inside a method body, `type
# (H) otherType string` inside a closure) is a node on both sides, but the
# (H) oracle emitted no containment for it while cgr parents it to the nearest
# (H) NODE-BEARING enclosing function (Go func literals have no nodes, and a
# (H) receiver method's node is receiver-qualified). Both sides must agree
# (H) (thrift lib/go surfaced 2 such edges as false positives).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from evals.cgr_graph import extract_cgr_go_graph
from evals.oracles import go_available, run_go_oracle
from evals.score import score_edge_types

GO_SRC = """package thrift

import (
	"encoding/json"
	"testing"
)

type SlogTStructWrapper struct {
	Value int
}

func (w SlogTStructWrapper) MarshalJSON() ([]byte, error) {
	type alias SlogTStructWrapper
	return json.Marshal(alias(w))
}

func TestHeaderContext(t *testing.T) {
	t.Run(
		"NoConflicts",
		func(t *testing.T) {
			type otherType string
			const otherValue = "bar2"
			_ = otherType(otherValue)
		},
	)
}
"""


@pytest.mark.skipif(not go_available(), reason="go toolchain not available")
def test_function_local_types_parent_to_enclosing_function(tmp_path: Path) -> None:
    (tmp_path / "wrapper_test.go").write_text(GO_SRC)

    cgr = extract_cgr_go_graph(tmp_path, tmp_path.name)
    oracle = run_go_oracle(tmp_path)

    result = score_edge_types(
        cgr,
        oracle,
        (cs.RelationshipType.DEFINES, cs.RelationshipType.DEFINES_METHOD),
    )
    for row in result.rows:
        assert row["fp"] == 0, result.rows
        assert row["fn"] == 0, result.rows
