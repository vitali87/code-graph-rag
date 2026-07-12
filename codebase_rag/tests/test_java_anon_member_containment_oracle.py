# (H) A method declared in an anonymous class body (`new Runnable(){ run(){} }`)
# (H) is modelled as a standalone Function by both cgr and the oracle, but cgr
# (H) anchors it to the module with DEFINES (the orphan-prevention fallback)
# (H) while the oracle emitted no containment at all, so every such member
# (H) graded as a false-positive edge on thrift (103 in crossTest). The
# (H) oracle must model the fallback.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from evals.cgr_graph import extract_cgr_java_graph
from evals.oracles import java_available, run_java_oracle
from evals.score import score_edge_types

JAVA_SRC = """\
package demo;

public class Client {
    public void start() {
        Runnable task = new Runnable() {
            public void run() {
                System.out.println("hi");
            }
        };
        task.run();
    }
}
"""


@pytest.mark.skipif(not java_available(), reason="javac toolchain not available")
def test_anonymous_class_members_anchor_to_module(tmp_path: Path) -> None:
    (tmp_path / "Client.java").write_text(JAVA_SRC)

    cgr = extract_cgr_java_graph(tmp_path, tmp_path.name)
    oracle = run_java_oracle(tmp_path)

    result = score_edge_types(
        cgr,
        oracle,
        (cs.RelationshipType.DEFINES, cs.RelationshipType.DEFINES_METHOD),
    )
    for row in result.rows:
        assert row["fp"] == 0, result.rows
        assert row["fn"] == 0, result.rows
