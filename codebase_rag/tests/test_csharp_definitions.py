# (H) C# Phase 1: definition extraction (namespaces, types, members, FQNs).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater
from codebase_rag.types_defs import NodeType

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_defs"
    project.mkdir()
    return project


def _endswith_any(names: set[str], suffix: str) -> bool:
    return any(n.endswith(suffix) for n in names)


def test_file_scoped_and_block_namespace_fold_identically(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Scoped.cs").write_text(
        "namespace Foo.Bar;\npublic class Widget { public void Run() {} }\n",
        encoding="utf-8",
    )
    (csharp_project / "Block.cs").write_text(
        "namespace Foo.Bar { public class Gadget { public void Run() {} } }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    methods = get_node_names(mock_ingestor, NodeType.METHOD) | get_node_names(
        mock_ingestor, NodeType.FUNCTION
    )
    # (H) Both spellings must place the type under the Foo.Bar namespace.
    assert _endswith_any(classes, "Foo.Bar.Widget")
    assert _endswith_any(classes, "Foo.Bar.Gadget")
    assert _endswith_any(methods, "Foo.Bar.Widget.Run")
    assert _endswith_any(methods, "Foo.Bar.Gadget.Run")


def test_type_declaration_kinds(csharp_project: Path, mock_ingestor: MagicMock) -> None:
    (csharp_project / "Types.cs").write_text(
        """
namespace N;
public class C { }
public struct S { }
public interface I { }
public enum E { A, B }
public record R(int X, int Y);
public record struct RS(int Z);
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # (H) class, struct and records map to Class; interface -> Interface;
    # (H) enum -> Enum (C# reuses determine_node_type's node-type mapping).
    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    for name in ("N.C", "N.S", "N.R", "N.RS"):
        assert _endswith_any(classes, name), f"missing type {name}: {classes}"
    assert _endswith_any(get_node_names(mock_ingestor, NodeType.INTERFACE), "N.I")
    assert _endswith_any(get_node_names(mock_ingestor, NodeType.ENUM), "N.E")


def test_members_generics_and_operators(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Members.cs").write_text(
        """
namespace N;
public class Box<T> {
    public Box(T value) { }
    public T Get<U>(U key) => default;
    public int Size { get; set; }
    public static Box<T> operator +(Box<T> a, Box<T> b) => a;
    ~Box() { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    members = get_node_names(mock_ingestor, NodeType.METHOD) | get_node_names(
        mock_ingestor, NodeType.FUNCTION
    )
    # (H) Generic type parameters must not leak into names; methods with
    # (H) parameters carry a signature so overloads stay distinct (Phase 3).
    assert _endswith_any(classes, "N.Box")
    assert _endswith_any(members, "N.Box.Get(U)")
    assert _endswith_any(members, "N.Box.Size")
    # (H) Constructor is named after the type; destructor is distinct from it.
    assert _endswith_any(members, "N.Box.Box(T)")
    # (H) Operators and destructors must register as members too. The operator
    # (H) has no `name` field (synthesize `operator_<symbol>` + signature so
    # (H) overloaded operators stay distinct); the destructor's identifier
    # (H) collides with the ctor unless prefixed with `~`.
    assert _endswith_any(members, "N.Box.operator_+(Box, Box)")
    assert _endswith_any(members, "N.Box.~Box")


def test_operator_overloads_and_conversions_are_distinct(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Ops.cs").write_text(
        """
namespace N;
public struct Vec {
    public int X;
    public static Vec operator +(Vec a, Vec b) => a;
    public static Vec operator +(Vec a, int b) => a;
    public static explicit operator int(Vec v) => v.X;
    public static implicit operator string(Vec v) => "";
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    members = get_node_names(mock_ingestor, NodeType.METHOD) | get_node_names(
        mock_ingestor, NodeType.FUNCTION
    )
    # (H) Two `operator +` overloads differ only by parameter type, so the
    # (H) signature must keep them as two nodes (not one @line-suffixed collision).
    assert _endswith_any(members, "N.Vec.operator_+(Vec, Vec)")
    assert _endswith_any(members, "N.Vec.operator_+(Vec, int)")
    # (H) Conversion operators are named by their target type.
    assert _endswith_any(members, "N.Vec.operator_int(Vec)")
    assert _endswith_any(members, "N.Vec.operator_string(Vec)")


def test_nested_types(csharp_project: Path, mock_ingestor: MagicMock) -> None:
    (csharp_project / "Nested.cs").write_text(
        "namespace N;\npublic class Outer { public class Inner { public void M() {} } }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    methods = get_node_names(mock_ingestor, NodeType.METHOD) | get_node_names(
        mock_ingestor, NodeType.FUNCTION
    )
    assert _endswith_any(classes, "N.Outer.Inner")
    assert _endswith_any(methods, "N.Outer.Inner.M")


def test_local_function_in_parameterized_method_parents_to_method(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A parameterized method registers with an overload-suffixed qn
    # (H) (`Run(int)`), so the local function's structurally re-derived parent
    # (H) (`Run`) misses the registry and the DEFINES edge silently degrades to
    # (H) the Module (Polly's BulkheadEngine shape). The recorded method
    # (H) identity must be reused instead.
    (csharp_project / "Engine.cs").write_text(
        """
namespace N;
public class Sample {
    public static int Run(int x) {
        int Local() { return 1; }
        return Local();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    defines = {
        (c.args[0][0], c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "DEFINES")
    }
    assert any(
        label == "Method" and parent.endswith("N.Sample.Run(int)")
        for label, parent, child in defines
        if child.endswith("Run.Local")
    ), defines
    assert not any(
        label == "Module" for label, _, child in defines if child.endswith("Run.Local")
    ), defines
