# (H) Covers the Java structure oracle harness (evals/oracles/java_oracle +
# (H) evals/java_l1.py): the JDK Compiler Tree API oracle is authoritative ground
# (H) truth, and cgr's captured Java nodes are graded against it on
# (H) (kind, file, start_line). Includes an anonymous class, whose methods cgr
# (H) models as standalone Functions (like JS object-literal methods).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_java_nodes
from evals.oracles import java_available, run_java_oracle
from evals.score import score_node_kinds
from evals.types_defs import GraphData

JAVA_SRC = """\
package demo;

public class Sample {
    private int x;
    public Sample(int x) { this.x = x; }
    public int area() { return x; }
    public static Sample make(int x) { return new Sample(x); }

    interface Shape { double area(); }
    enum Color { RED, GREEN }
    static class Inner { void helper() {} }

    Runnable callback() {
        return new Runnable() {
            public void run() { helper2(); }
            void helper2() {}
        };
    }
}

interface Drawable { void draw(); }

enum Direction { NORTH, SOUTH }
"""


def _require_java() -> None:
    if not java_available():
        pytest.skip("javac/java toolchain not available")
    if cs.SupportedLanguage.JAVA not in load_parsers()[0]:
        pytest.skip("java parser not available")


def test_cgr_matches_jdk_oracle_on_java_structure(tmp_path: Path) -> None:
    _require_java()
    project = tmp_path / "java_oracle_test"
    project.mkdir()
    (project / "Sample.java").write_text(JAVA_SRC, encoding="utf-8")

    cgr = GraphData(
        nodes=extract_cgr_java_nodes(project, project.name),
        edges=set(),
        name_edges=set(),
    )
    oracle = run_java_oracle(project)

    result = score_node_kinds(cgr, oracle, ec.JAVA_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("Class", "Interface", "Enum", "Method", "Function"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)
