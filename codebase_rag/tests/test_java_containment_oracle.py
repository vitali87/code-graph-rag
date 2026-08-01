# Covers Java containment-edge validation: cgr's DEFINES (file module ->
# every named type, including nested) and DEFINES_METHOD (class/interface/
# enum -> method) edges are graded against the independent JDK Compiler Tree
# API oracle, joined on (kind, file, line). Exercises an interface method, an
# enum method, and a nested class (cgr keeps type containment flat).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_java_graph
from evals.oracles import java_available, run_java_oracle
from evals.score import score_edge_types

JAVA_SRC = """\
package demo;

public interface Shape {
    double area();
}

public enum Color {
    RED, GREEN;
    public int rank() { return 1; }
}

public class Point implements Shape {
    private int x;
    public double area() { return 1.0; }

    public static class Inner {
        public void helper() {}
    }
}
"""


def _require_java() -> None:
    if not java_available():
        pytest.skip("java toolchain not available")
    if cs.SupportedLanguage.JAVA not in load_parsers()[0]:
        pytest.skip("java parser not available")


def test_cgr_matches_jdk_oracle_on_containment_edges(tmp_path: Path) -> None:
    _require_java()
    project = tmp_path / "java_edge_test"
    project.mkdir()
    (project / "Demo.java").write_text(JAVA_SRC, encoding="utf-8")

    cgr = extract_cgr_java_graph(project, project.name)
    oracle = run_java_oracle(project)

    result = score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in (
        cs.RelationshipType.DEFINES.value,
        cs.RelationshipType.DEFINES_METHOD.value,
    ):
        row = by_label.get(label)
        assert row is not None, (label, by_label, result.diff)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (
            label,
            row,
            result.diff,
        )
