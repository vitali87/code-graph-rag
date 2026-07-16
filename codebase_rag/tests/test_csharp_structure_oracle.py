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
public interface IBig : IDrawable { }
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


def test_oracle_ignores_cgr_ignored_dirs_for_classification(
    tmp_path: Path,
) -> None:
    # (H) A type declared only under a cgr-ignored directory (`.venv/`, absent from
    # (H) the oracle's hardcoded fallback set) must not enter the oracle's declared
    # (H) universe: otherwise a base `Drawable` there would flip a real class's edge
    # (H) from INHERITS to IMPLEMENTS, diverging from cgr (which never indexes the
    # (H) ignored file). The oracle skips it via cgr's IGNORE_PATTERNS, so the real
    # (H) `Widget : Drawable` stays INHERITS and no ignored-dir node is emitted.
    _require_csharp()
    project = tmp_path / "csharp_ignore"
    project.mkdir()
    (project / "App.cs").write_text(
        "namespace N;\npublic class Widget : Drawable { }\n", encoding="utf-8"
    )
    hidden = project / ".venv"
    hidden.mkdir()
    (hidden / "Gen.cs").write_text(
        "namespace N;\npublic interface Drawable { }\n", encoding="utf-8"
    )
    oracle = run_csharp_oracle(project)
    assert not any(n.file.startswith(".venv") for n in oracle.nodes), oracle.nodes
    rels = {(e.rel_type, e.target_name) for e in oracle.name_edges}
    assert (cs.RelationshipType.INHERITS.value, "Drawable") in rels, rels
    assert (cs.RelationshipType.IMPLEMENTS.value, "Drawable") not in rels, rels


def test_oracle_interface_bases_are_inherits(tmp_path: Path) -> None:
    # (H) An interface extending an interface is INHERITS in cgr's model (the
    # (H) Java oracle already matches this), so the oracle must classify a base
    # (H) by the DECLARING type, not by the base's own kind; otherwise every
    # (H) interface-to-interface edge grades as a false positive (26 on Polly).
    _require_csharp()
    project = tmp_path / "csharp_iface_bases"
    project.mkdir()
    (project / "I.cs").write_text(
        "namespace N;\n"
        "public interface IShape { }\n"
        "public interface IExtended : IShape { }\n",
        encoding="utf-8",
    )
    oracle = run_csharp_oracle(project)
    rels = {(e.rel_type, e.target_name) for e in oracle.name_edges}
    assert (cs.RelationshipType.INHERITS.value, "IShape") in rels, rels
    assert (cs.RelationshipType.IMPLEMENTS.value, "IShape") not in rels, rels


def test_same_scope_arity_pair_inherits_grades_clean(tmp_path: Path) -> None:
    # (H) Issue #764: a same-file arity pair registers the second type as a
    # (H) DUP_QN_MARKER variant (ITtl@3); the recovered INHERITS edge targets
    # (H) that variant qn, and the eval's simple-name reduction must strip the
    # (H) marker or the true edge grades as one fp + one fn against the
    # (H) oracle's clean base name.
    _require_csharp()
    project = tmp_path / "csharp_arity_pair"
    project.mkdir()
    (project / "Ttl.cs").write_text(
        "namespace N;\n"
        "public interface ITtl : ITtl<object> { }\n"
        "public interface ITtl<TResult> { int GetTtl(); }\n",
        encoding="utf-8",
    )
    cgr = extract_cgr_csharp_graph(project, project.name)
    oracle = run_csharp_oracle(project)
    result = score_name_edge_types(cgr, oracle, ec.INHERITANCE_NAME_EDGE_TYPES)
    by_label = {row["label"]: row for row in result.rows}
    row = by_label.get("INHERITS")
    assert row is not None, by_label
    assert row["precision"] == 1.0 and row["recall"] == 1.0, row


def test_oracle_suppresses_preprocessor_split_phantom_members(
    tmp_path: Path,
) -> None:
    # (H) Issue #768: an expression-bodied member whose body is split across
    # (H) #if/#else has TWO bodies once the directives are neutralized, which is
    # (H) ill-formed C#; Roslyn ends the member at the first branch's `;` and
    # (H) error-recovers the second branch's expression as a phantom member
    # (H) declaration (a Method named `Substring` on Polly's KeyHelper). Error
    # (H) recovery artifacts are never something cgr should be graded against,
    # (H) so the oracle must drop them while keeping the real member.
    _require_csharp()
    project = tmp_path / "csharp_split_body"
    project.mkdir()
    (project / "KeyHelper.cs").write_text(
        "using System;\n"
        "namespace N;\n"
        "public static class KeyHelper\n"
        "{\n"
        "    private const int GuidPartLength = 8;\n"
        "\n"
        "    public static string GuidPart() =>\n"
        "#if NET6_0_OR_GREATER\n"
        "        Guid.NewGuid().ToString()[..GuidPartLength];\n"
        "#else\n"
        "        Guid.NewGuid().ToString().Substring(0, GuidPartLength);\n"
        "#endif\n"
        "}\n",
        encoding="utf-8",
    )
    oracle = run_csharp_oracle(project)
    names = {n.name for n in oracle.nodes.values()}
    assert "GuidPart" in names, names
    assert "Substring" not in names, names


def test_oracle_anchors_top_level_functions_to_module(tmp_path: Path) -> None:
    # (H) A Cake-style build script declares functions at the top level
    # (H) (local functions of the implicit main); cgr anchors them
    # (H) Module -> Function, so the oracle must emit the same containment
    # (H) instead of nothing (which graded cgr's correct edges as false
    # (H) positives on Polly's cake.cs).
    _require_csharp()
    project = tmp_path / "csharp_script"
    project.mkdir()
    (project / "build.cs").write_text(
        "int Twice(int x) => 2 * x;\nSystem.Console.WriteLine(Twice(21));\n",
        encoding="utf-8",
    )
    cgr = extract_cgr_csharp_graph(project, project.name)
    oracle = run_csharp_oracle(project)
    wanted = {
        e
        for e in oracle.edges
        if e.rel_type == cs.RelationshipType.DEFINES.value
        and e.child.kind == cs.NodeLabel.FUNCTION.value
    }
    assert {
        (e.parent.kind, e.parent.start_line, e.child.start_line) for e in wanted
    } == {(cs.NodeLabel.MODULE.value, 0, 1)}, oracle.edges
    assert wanted <= cgr.edges, {"oracle_only": wanted - cgr.edges}


def test_oracle_includes_declarations_in_inactive_if_regions(
    tmp_path: Path,
) -> None:
    # (H) cgr has no preprocessor: it parses every `#if`/`#else` branch, so a method
    # (H) guarded by an undefined symbol IS in cgr's graph. The Roslyn oracle must
    # (H) match that view (neutralize conditional directives so all branches parse),
    # (H) otherwise real declarations inside `#if` blocks read as cgr false positives
    # (H) (e.g. ~871 phantom Method FPs on Newtonsoft.Json).
    _require_csharp()
    project = tmp_path / "csharp_if"
    project.mkdir()
    (project / "Guarded.cs").write_text(
        "namespace N;\n"
        "public class Guarded {\n"
        "    public void Active() { }\n"
        "#if HAVE_SOMETHING\n"
        "    public void OnlyWhenDefined() { }\n"
        "#endif\n"
        "}\n",
        encoding="utf-8",
    )
    cgr = extract_cgr_csharp_graph(project, project.name)
    oracle = run_csharp_oracle(project)
    oracle_keys = {(n.kind, n.start_line) for n in oracle.nodes}
    cgr_keys = {(n.kind, n.start_line) for n in cgr.nodes}
    # (H) OnlyWhenDefined() is on line 5, inside the undefined `#if HAVE_SOMETHING`.
    assert (cs.NodeLabel.METHOD.value, 5) in oracle_keys, oracle_keys
    assert cgr_keys == oracle_keys, {
        "cgr_only": cgr_keys - oracle_keys,
        "oracle_only": oracle_keys - cgr_keys,
    }


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
