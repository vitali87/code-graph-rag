# Covers Java node SPAN (end_line) validation: cgr's end_line for each node is
# graded against the JDK Compiler Tree API oracle (which emits each node's
# source end position), joined on (kind, file, start). Exercises a class with a
# multi-line method signature, an interface, an enum, and a nested class so
# spans are not trivially single line.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_java_graph
from evals.oracles import java_available, run_java_oracle
from evals.score import score_span

JAVA_SRC = """\
package demo;

public class Widget implements Shape {
    private int size;

    public int area(
        int scale
    ) {
        return this.size * scale;
    }

    static class Inner {
        int value() {
            return 1;
        }
    }
}

interface Shape {
    int area(int scale);
}

enum Color {
    RED,
    GREEN,
    BLUE
}
"""


def _require_java() -> None:
    if not java_available():
        pytest.skip("jdk (javac/java) not available")
    if cs.SupportedLanguage.JAVA not in load_parsers()[0]:
        pytest.skip("java parser not available")


def test_cgr_matches_jdk_oracle_on_node_spans(tmp_path: Path) -> None:
    _require_java()
    project = tmp_path / "java_span_test"
    (project / "demo").mkdir(parents=True)
    (project / "demo" / "Widget.java").write_text(JAVA_SRC, encoding="utf-8")

    cgr = extract_cgr_java_graph(project, project.name)
    oracle = run_java_oracle(project)

    result = score_span(cgr, oracle, ec.JAVA_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    aggregate = by_label.get(ec.AGGREGATE_LABEL)
    assert aggregate is not None, (by_label, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )
    assert aggregate["tp"] >= 5, aggregate
