"""Issue #2922: a variable typed by an enum nested in a class is typed.

The type resolver mapped a simple type name to a type declared in the same
file only when that type was a class or an interface, so a nested enum's
name stayed unresolved: every method call on a parameter, local or field of
that type bound nothing, and the enum's methods were reported dead (gson:
61 `factory.create(...)` sites through a nested `enum Factory`).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

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
    }

    static class Box { int size() { return 1; } }
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

PREFIX = "src.main.java.demo"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("jenum") / "jenum"
    _write(root, f"{PREFIX.replace('.', '/')}/Outer.java", OUTER)
    _write(root, f"{PREFIX.replace('.', '/')}/Color.java", COLOR)
    _write(root, f"{PREFIX.replace('.', '/')}/Scoped.java", SCOPED)
    _write(root, f"{PREFIX.replace('.', '/')}/Parent.java", PARENT)
    _write(root, f"{PREFIX.replace('.', '/')}/Outer2.java", OUTER2)
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
        ("Outer2.Outer2.Child.use(Kind)", "Parent.Parent.Kind.code()"),
    ],
    ids=[
        "enclosing-class-member",
        "inner-scope-enum",
        "local-typed-by-its-initializer",
        "inherited-member-type",
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
