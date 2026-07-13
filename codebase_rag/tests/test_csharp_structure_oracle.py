# (H) Covers the C# structure oracle harness (evals/oracles/csharp_oracle +
# (H) evals/csharp_l1.py): the Roslyn syntax-tree oracle is authoritative ground
# (H) truth, and cgr's captured C# graph is graded against it on
# (H) (kind, file, start_line) for nodes, on containment edges, on inheritance
# (H) name-edges, and on end_line spans. Covers class/struct/record -> Class,
# (H) interface, enum, members -> Method, a local function -> Function, a nested
# (H) type, an attribute-prefixed member (start line includes the attribute), and
# (H) base class vs interface split.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals.cgr_graph import extract_cgr_csharp_graph, extract_cgr_csharp_nodes
from evals.oracles import csharp_oracle_available, run_csharp_oracle
from evals.score import (
    score_edge_types,
    score_name_edge_types,
    score_node_kinds,
    score_span,
)
from evals.types_defs import GraphData

CSHARP_SRC = """\
namespace Demo;

public class Sample {
    private int x;
    public Sample(int x) { this.x = x; }
    public int Area() { return x; }
    public static Sample Make(int x) => new Sample(x);
    ~Sample() { }
    public int Prop { get; set; }
    public static Sample operator +(Sample a, Sample b) => a;

    [System.Obsolete]
    public int Tagged() { int Local() => 1; return Local(); }

    interface IShape { double Area(); }
    enum Color { Red, Green }
    struct Point { public int X; }
    record Pair(int A, int B);
}

public interface IDrawable { void Draw(); }
public enum Direction { North, South }
public struct Vec { public int X; }
public record Person(string Name);

public class Dog : Animal, IDrawable {
    public void Draw() { }
}
public class Animal { }
"""


def _require_csharp() -> None:
    if not csharp_oracle_available():
        pytest.skip("dotnet toolchain not available")
    if cs.SupportedLanguage.CSHARP not in load_parsers()[0]:
        pytest.skip("c_sharp parser not available")


@pytest.fixture
def graphs(tmp_path: Path) -> tuple[GraphData, GraphData]:
    _require_csharp()
    project = tmp_path / "csharp_oracle_test"
    project.mkdir()
    (project / "Sample.cs").write_text(CSHARP_SRC, encoding="utf-8")
    cgr = extract_cgr_csharp_graph(project, project.name)
    oracle = run_csharp_oracle(project)
    return cgr, oracle


def test_cgr_matches_roslyn_oracle_on_nodes(
    graphs: tuple[GraphData, GraphData],
) -> None:
    cgr, oracle = graphs
    node_only = GraphData(nodes=oracle.nodes, edges=set(), name_edges=set())
    result = score_node_kinds(cgr, node_only, ec.CSHARP_SCORED_NODE_KINDS)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("Class", "Interface", "Enum", "Method", "Function"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)


def test_oracle_agrees_with_cgr_nodes_exactly(
    tmp_path: Path,
) -> None:
    # (H) A record/struct is a Class, an interface an Interface, an enum an Enum,
    # (H) a local function a Function, and the [Obsolete]-tagged member's start
    # (H) line is the attribute line -- assert the raw (kind, line) sets agree so a
    # (H) label or line-convention drift on either side is caught directly.
    _require_csharp()
    project = tmp_path / "csharp_nodes"
    project.mkdir()
    (project / "Sample.cs").write_text(CSHARP_SRC, encoding="utf-8")
    cgr_nodes = extract_cgr_csharp_nodes(project, project.name)
    oracle = run_csharp_oracle(project)
    cgr_keys = {(k.kind, k.start_line) for k in cgr_nodes}
    oracle_keys = {(k.kind, k.start_line) for k in oracle.nodes}
    assert cgr_keys == oracle_keys, {
        "cgr_only": cgr_keys - oracle_keys,
        "oracle_only": oracle_keys - cgr_keys,
    }


def test_cgr_matches_roslyn_oracle_on_containment(
    graphs: tuple[GraphData, GraphData],
) -> None:
    cgr, oracle = graphs
    result = score_edge_types(cgr, oracle, ec.SCORED_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("DEFINES", "DEFINES_METHOD"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)


def test_cgr_matches_roslyn_oracle_on_inheritance(
    graphs: tuple[GraphData, GraphData],
) -> None:
    # (H) Dog : Animal, IDrawable -> INHERITS Animal (base class) and IMPLEMENTS
    # (H) IDrawable (interface); the oracle splits by what the sources declare.
    cgr, oracle = graphs
    result = score_name_edge_types(cgr, oracle, ec.INHERITANCE_NAME_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    for label in ("INHERITS", "IMPLEMENTS"):
        row = by_label.get(label)
        assert row is not None, (label, by_label)
        assert row["precision"] == 1.0 and row["recall"] == 1.0, (label, row)


def test_cgr_matches_roslyn_oracle_on_spans(
    graphs: tuple[GraphData, GraphData],
) -> None:
    # (H) score_span grades each matched def's end_line: a per-label row scores
    # (H) 1.0 only when cgr's node span (end_line) equals the oracle's, so this
    # (H) asserts the declaration extents agree, not just the start lines.
    cgr, oracle = graphs
    result = score_span(cgr, oracle, ec.CSHARP_SCORED_NODE_KINDS)
    assert result.rows, "no span rows produced"
    for row in result.rows:
        assert row["precision"] == 1.0 and row["recall"] == 1.0, row
