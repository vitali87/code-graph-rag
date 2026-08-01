# Covers Java inheritance-edge validation: cgr's INHERITS (class/interface
# extends) and IMPLEMENTS (class/enum implements) edges are graded against the
# JDK Compiler Tree API oracle, by (source node, base SIMPLE NAME).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_java_graph
from evals.oracles import java_available, run_java_oracle
from evals.score import score_name_edge_types

JAVA_SRC = """\
package demo;

public interface A {}
public interface B {}
public interface Big extends A, B {}

abstract class Base {}
enum Color implements A { RED }

class Circle extends Base implements A, B {}
"""


def _require_java() -> None:
    if not java_available():
        pytest.skip("java toolchain not available")
    if cs.SupportedLanguage.JAVA not in load_parsers()[0]:
        pytest.skip("java parser not available")


def test_cgr_matches_jdk_oracle_on_inheritance_edges(tmp_path: Path) -> None:
    _require_java()
    project = tmp_path / "java_inh_edge"
    project.mkdir()
    (project / "Demo.java").write_text(JAVA_SRC, encoding="utf-8")

    cgr = extract_cgr_java_graph(project, project.name)
    oracle = run_java_oracle(project)

    result = score_name_edge_types(cgr, oracle, ec.INHERITANCE_NAME_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in (
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.IMPLEMENTS.value,
    ):
        row = by_label.get(label)
        assert row is not None, (label, by_label, result.diff)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (
            label,
            row,
            result.diff,
        )
