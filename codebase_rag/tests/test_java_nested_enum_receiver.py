"""Issue #2922: a variable typed by an enum nested in a class is typed.

The type resolver mapped a simple type name to a type declared in the same
file only when that type was a class or an interface, so a nested enum's
name stayed unresolved: every method call on a parameter, local or field of
that type bound nothing, and the enum's methods were reported dead (gson:
61 `factory.create(...)` sites through a nested `enum Factory`).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Node, Parser

from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.java.type_inference import JavaTypeInferenceEngine
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import NodeType

OUTER = """\
package demo;

public final class Outer {
    static class Box { int size() { return 1; } }

    enum Kind {
        A, B;
        int code() { return ordinal(); }
    }

    enum Factory {
        PLAIN {
            @Override String create() { return "plain"; }
        };
        abstract String create();
    }

    private Kind field = Kind.A;

    public static int useKind(Kind kind) { return kind.code(); }

    public static int useLocalEnum() { Kind k = Kind.A; return k.code(); }

    public int useField() { return field.code(); }

    public static String useFactory(Factory factory) { return factory.create(); }

    public static int useBox(Box box) { return box.size(); }

    public static String useColor(Color c) { return c.hex(); }
}
"""

COLOR = """\
package demo;

public enum Color {
    RED;
    String hex() { return "#f00"; }
}
"""

# `Helper` names two nested types: the class a member of `Scoped`, and an
# enum a member of `Scoped.Other`. Java resolves each use to the member of
# the innermost enclosing type declaring it.
SCOPED = """\
package demo;

public final class Scoped {
    static class Helper { boolean check() { return true; } }

    public static boolean use(Helper h) { return h.check(); }

    static class Other {
        enum Helper {
            X;
            boolean check() { return false; }
        }

        static boolean useInner(Helper h) { return h.check(); }

        static class Box { int size() { return 2; } }

        static int viaNew() { Box b = new Box(); return b.size(); }

        static class Bag<T> { int count() { return 2; } }

        static int viaGeneric(Bag<String> bag) { return bag.count(); }

        static int viaArray(Box[] boxes) { return boxes[0].size(); }
    }

    static class Box { int size() { return 1; } }

    static class Bag<T> { int count() { return 1; } }
}
"""

# `Kind` inside Child is the member Child inherits from Parent, ahead of
# the one its enclosing Outer2 declares (JLS 8.5: inherited member types).
PARENT = """\
package demo;

public class Parent {
    public enum Kind { A; int code() { return 1; } }
}
"""
OUTER2 = """\
package demo;

public class Outer2 {
    enum Kind { B; int code() { return 2; } }

    static class Child extends Parent {
        int use(Kind k) { return k.code(); }
    }
}
"""

# A member type inherited from an implemented interface wins the same way
# (JLS 8.5), whether that interface is in another file or nested in the
# enclosing class itself.
IFACE = """\
package demo;

public interface Iface {
    enum Kind { A; int code() { return 1; } }
}
"""
OUTER3 = """\
package demo;

public class Outer3 {
    enum Kind { B; int code() { return 2; } }

    interface Local {
        enum Kind { C; int code() { return 3; } }
    }

    static class Child implements Iface {
        int use(Kind k) { return k.code(); }
    }

    static class Sibling implements Local {
        int use(Kind k) { return k.code(); }
    }
}
"""

PREFIX = "src.main.java.demo"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("jenum") / "jenum"
    _write(root, f"{PREFIX.replace('.', '/')}/Outer.java", OUTER)
    _write(root, f"{PREFIX.replace('.', '/')}/Color.java", COLOR)
    _write(root, f"{PREFIX.replace('.', '/')}/Scoped.java", SCOPED)
    _write(root, f"{PREFIX.replace('.', '/')}/Parent.java", PARENT)
    _write(root, f"{PREFIX.replace('.', '/')}/Outer2.java", OUTER2)
    _write(root, f"{PREFIX.replace('.', '/')}/Iface.java", IFACE)
    _write(root, f"{PREFIX.replace('.', '/')}/Outer3.java", OUTER3)
    return _index(root, MagicMock())


def _calls_from(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}.{PREFIX}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}{caller}"
    }


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    return _calls_from(graph, f"Outer.Outer.{caller}")


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("useKind(Kind)", "Outer.Outer.Kind.code()"),
        ("useLocalEnum()", "Outer.Outer.Kind.code()"),
        ("useField()", "Outer.Outer.Kind.code()"),
    ],
    ids=["parameter", "local", "field"],
)
def test_a_nested_enum_receiver_binds_its_method(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _callees(graph, caller).get(callee) == "exact"


def test_a_nested_enum_with_constant_bodies_binds_both_definitions(
    graph: RecordedGraph,
) -> None:
    # The constant's body overrides the abstract method; the call can run
    # either, so it fans out to both (gson's `JsonReaderPathTest.Factory`).
    assert _callees(graph, "useFactory(Factory)") == {
        "Outer.Outer.Factory.create()": "overload",
        "Outer.Outer.Factory.create()@15": "overload",
    }


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("Scoped.Scoped.use(Helper)", "Scoped.Scoped.Helper.check()"),
        (
            "Scoped.Scoped.Other.useInner(Helper)",
            "Scoped.Scoped.Other.Helper.check()",
        ),
        ("Scoped.Scoped.Other.viaNew()", "Scoped.Scoped.Other.Box.size()"),
        (
            "Scoped.Scoped.Other.viaGeneric(Bag<String>)",
            "Scoped.Scoped.Other.Bag.count()",
        ),
        ("Outer2.Outer2.Child.use(Kind)", "Parent.Parent.Kind.code()"),
        ("Outer3.Outer3.Child.use(Kind)", "Iface.Iface.Kind.code()"),
        ("Outer3.Outer3.Sibling.use(Kind)", "Outer3.Outer3.Local.Kind.code()"),
    ],
    ids=[
        "enclosing-class-member",
        "inner-scope-enum",
        "local-typed-by-its-initializer",
        "generic-parameter",
        "inherited-member-type",
        "implemented-interface-member-type",
        "nested-interface-member-type",
    ],
)
def test_a_same_named_type_resolves_in_the_callers_scope(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _calls_from(graph, caller) == {callee: "exact"}


# Negative: what must not change.


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("useBox(Box)", "Outer.Outer.Box.size()"),
        ("useColor(Color)", "Color.Color.hex()"),
    ],
    ids=["nested-class", "top-level-enum"],
)
def test_a_nested_class_or_top_level_enum_still_binds(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _callees(graph, caller).get(callee) == "exact"


def _method_body(node: Node, name: str) -> Node | None:
    if node.type == "method_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.text == name.encode():
            return node.child_by_field_name("body")
    for child in node.children:
        if (found := _method_body(child, name)) is not None:
            return found
    return None


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("Bag<String>", "Scoped.Other.Bag"),
        ("Box[]", "Scoped.Other.Box[]"),
    ],
    ids=["generic", "array"],
)
def test_a_generic_or_array_type_resolves_in_its_scope(
    type_name: str, expected: str
) -> None:
    # The generic and array branches strip `<...>` / `[]` and resolve the
    # base name again; that resolution must keep the reading scope, or the
    # base, declared in both Scoped and Scoped.Other, is ambiguous file-wide.
    tsjava = pytest.importorskip("tree_sitter_java")
    module_qn = "proj.Scoped"
    registry = FunctionRegistryTrie()
    for qn in (
        "Scoped",
        "Scoped.Box",
        "Scoped.Bag",
        "Scoped.Other",
        "Scoped.Other.Box",
        "Scoped.Other.Bag",
    ):
        registry[f"{module_qn}.{qn}"] = NodeType.CLASS
    engine = JavaTypeInferenceEngine(
        import_processor=ImportProcessor(repo_path=Path("/r"), project_name="proj"),
        function_registry=registry,
        repo_path=Path("/r"),
        project_name="proj",
        ast_cache=MagicMock(),
        queries={},
        module_qn_to_file_path={},
        class_inheritance={},
        simple_name_lookup=defaultdict(set),
    )
    tree = Parser(Language(tsjava.language())).parse(SCOPED.encode())
    scope = _method_body(tree.root_node, "viaArray")
    assert scope is not None
    assert (
        engine._resolve_java_type_name(type_name, module_qn, scope)
        == f"{module_qn}.{expected}"
    )
