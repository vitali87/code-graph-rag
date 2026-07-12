# (H) Covers the C++ structure oracle (evals/oracles/cpp_oracle.py): a libclang
# (H) oracle driven by a compile_commands.json resolves #includes and expands
# (H) macros to the true translation-unit AST, which tree-sitter cannot do. cgr's
# (H) C++ nodes, containment edges, and spans are graded against it on
# (H) (kind, file, start_line). The sample exercises a header-declared class
# (H) (resolved via an -I include path), a macro-typed method, out-of-class method
# (H) definitions, a constructor, an inline method, a struct, and a free function.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_cpp_graph, restrict_to_files
from evals.oracles import cpp_available, run_cpp_oracle
from evals.score import (
    score_edge_types,
    score_name_edge_types,
    score_node_kinds,
    score_span,
)
from evals.types_defs import (
    DefNode,
    EdgeKey,
    GraphData,
    NameEdge,
    NodeKey,
    ScoreRow,
)

SHAPE_H = """\
#pragma once
#define AREA_T double

struct Point {
    int x;
    int y;
};

class Shape {
public:
    Shape(int id);
    AREA_T area() const;
    void scale(
        double factor
    );
    int inline_id() const { return id_; }
private:
    int id_;
};
"""

SHAPE_CPP = """\
#include "shape.h"

Shape::Shape(int id) : id_(id) {
}

AREA_T Shape::area() const {
    return 1.0;
}

void Shape::scale(double factor) {
    id_ = static_cast<int>(factor);
}

int helper(int n) {
    return n * 2;
}
"""


def _require_cpp() -> None:
    if not cpp_available():
        pytest.skip("libclang not available")
    if cs.SupportedLanguage.CPP not in load_parsers()[0]:
        pytest.skip("cpp parser not available")


def _aggregate(rows: list[ScoreRow]) -> ScoreRow | None:
    return next((r for r in rows if r["label"] == ec.AGGREGATE_LABEL), None)


def test_cgr_matches_libclang_oracle_on_cpp_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_cpp()
    # (H) The L1 structure eval grades the TREE-SITTER extraction against the
    # (H) libclang oracle; the default HYBRID frontend would add macro Function
    # (H) nodes the oracle does not model (the fixture's compdb triggers it).
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.TREESITTER)
    project = tmp_path / "cpp_proj"
    (project / "include").mkdir(parents=True)
    (project / "src").mkdir(parents=True)
    (project / "include" / "shape.h").write_text(SHAPE_H, encoding="utf-8")
    (project / "src" / "shape.cpp").write_text(SHAPE_CPP, encoding="utf-8")

    src = (project / "src" / "shape.cpp").resolve()
    include = (project / "include").resolve()
    compdb = [
        {
            "directory": str(project.resolve()),
            "file": str(src),
            "command": f"clang++ -std=c++17 -I{include} -c {src}",
        }
    ]
    (project / ec.CPP_COMPDB_FILENAME).write_text(json.dumps(compdb), encoding="utf-8")

    cgr = extract_cgr_cpp_graph(project, project.name)
    oracle = run_cpp_oracle(project)

    for label, result in (
        ("nodes", score_node_kinds(cgr, oracle, ec.CPP_SCORED_NODE_KINDS)),
        ("edges", score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)),
        ("spans", score_span(cgr, oracle, ec.CPP_SCORED_NODE_KINDS)),
    ):
        aggregate = _aggregate(result.rows)
        assert aggregate is not None, (label, result.rows, result.diff)
        assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
            label,
            aggregate,
            result.diff,
        )
    # (H) Guard the sample is non-trivial (class + struct + 4 methods + function).
    node_aggregate = _aggregate(
        score_node_kinds(cgr, oracle, ec.CPP_SCORED_NODE_KINDS).rows
    )
    assert node_aggregate is not None and node_aggregate["tp"] >= 7, node_aggregate


INHERIT_H = """\
#pragma once
struct Base { int v; };
struct Derived : public Base {
    int w;
};
"""

INHERIT_CPP = """\
#include "shapes.h"

int use(Derived d) {
    return d.v + d.w;
}
"""


def test_libclang_oracle_emits_inherits_edges(tmp_path: Path) -> None:
    # (H) The oracle must emit a base-class (CXX_BASE_SPECIFIER) edge as an INHERITS
    # (H) name edge keyed by the base's simple name, matching cgr; otherwise cgr's
    # (H) real inheritance edges are graded against an empty oracle set (all fp).
    _require_cpp()
    project = tmp_path / "inh_proj"
    (project / "include").mkdir(parents=True)
    (project / "src").mkdir(parents=True)
    (project / "include" / "shapes.h").write_text(INHERIT_H, encoding="utf-8")
    (project / "src" / "use.cpp").write_text(INHERIT_CPP, encoding="utf-8")

    src = (project / "src" / "use.cpp").resolve()
    include = (project / "include").resolve()
    compdb = [
        {
            "directory": str(project.resolve()),
            "file": str(src),
            "command": f"clang++ -std=c++17 -I{include} -c {src}",
        }
    ]
    (project / ec.CPP_COMPDB_FILENAME).write_text(json.dumps(compdb), encoding="utf-8")

    cgr = extract_cgr_cpp_graph(project, project.name)
    oracle = run_cpp_oracle(project)

    result = score_name_edge_types(cgr, oracle, ec.INHERITANCE_NAME_EDGE_TYPES)
    aggregate = _aggregate(result.rows)
    assert aggregate is not None, (result.rows, result.diff)
    assert aggregate["tp"] >= 1, (aggregate, result.diff)
    assert aggregate["precision"] == 1.0 and aggregate["recall"] == 1.0, (
        aggregate,
        result.diff,
    )


def test_restrict_to_files_scopes_graph_to_universe() -> None:
    # (H) Scale grading over a compile_commands.json must score cgr only on the
    # (H) files the oracle actually compiled; restrict_to_files drops cgr nodes,
    # (H) edges, and name edges that touch any out-of-universe file.
    keep = "include/a.h"
    drop = "test/gtest.h"
    mod_keep = NodeKey(cs.NodeLabel.MODULE.value, keep, ec.MODULE_START_LINE)
    cls_keep = NodeKey(cs.NodeLabel.CLASS.value, keep, 3)
    cls_drop = NodeKey(cs.NodeLabel.CLASS.value, drop, 5)
    graph = GraphData(
        nodes={
            cls_keep: DefNode(cls_keep, "Keep", 9),
            cls_drop: DefNode(cls_drop, "Drop", 11),
        },
        edges={
            EdgeKey(cs.RelationshipType.DEFINES.value, mod_keep, cls_keep),
            EdgeKey(
                cs.RelationshipType.DEFINES.value,
                NodeKey(cs.NodeLabel.MODULE.value, drop, ec.MODULE_START_LINE),
                cls_drop,
            ),
        },
        name_edges={
            NameEdge(cs.RelationshipType.INHERITS.value, cls_keep, "Other"),
            NameEdge(cs.RelationshipType.INHERITS.value, cls_drop, "Other"),
        },
    )

    scoped = restrict_to_files(graph, {keep})

    assert set(scoped.nodes) == {cls_keep}
    assert all(e.parent.file == keep and e.child.file == keep for e in scoped.edges)
    assert len(scoped.edges) == 1
    assert {n.source.file for n in scoped.name_edges} == {keep}
    assert len(scoped.name_edges) == 1
