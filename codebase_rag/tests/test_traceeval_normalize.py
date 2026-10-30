"""TraceEval scoring: the paper's normalization and the static protocol.

The normalization must behave exactly like the benchmark's own
`normalize_edges.py`, or a row here stops reading against the paper's
Table 3; each mode gets the cases its docstring there promises. The static
protocol gets the tracer artifacts it exists to remove.
"""

from __future__ import annotations

from pathlib import Path

from evals.traceeval import constants as tc
from evals.traceeval.normalize import normalize_edges, normalize_name
from evals.traceeval.protocol import (
    drop_class_bodies,
    is_anonymous,
    python_class_names,
    see_through_anonymous,
)

PY, JS, JAVA = tc.Language.PYTHON, tc.Language.JAVASCRIPT, tc.Language.JAVA
ALL = tc.ALL_NORM_MODES


def test_init_collapses_constructor_spellings() -> None:
    assert normalize_name("main.Foo.__init__", PY, ALL) == "main.Foo"
    assert normalize_name("main.Foo.__new__", PY, ALL) == "main.Foo"
    assert normalize_name("main.Foo.constructor", JS, ALL) == "main.Foo"
    assert normalize_name("pkg.Foo:<init>(int,int)", JAVA, ALL) == "pkg.Foo"
    assert normalize_name("pkg.Foo:<clinit>()", JAVA, ALL) == "pkg.Foo"
    # Only the language's own constructor spelling collapses.
    assert normalize_name("main.Foo.constructor", PY, ALL) == "main.Foo.constructor"


def test_nested_class_keeps_the_innermost_java_class() -> None:
    assert normalize_name("pkg.Outer.Inner:m(int)", JAVA, ALL) == "pkg.Inner:m(int)"
    assert normalize_name("pkg.Outer$Inner:m()", JAVA, ALL) == "pkg.Inner:m()"
    assert normalize_name("pkg.Only:m()", JAVA, ALL) == "pkg.Only:m()"


def test_stdlib_endpoints_are_dropped() -> None:
    assert normalize_name("print", PY, ALL) is None
    assert normalize_name("os.path.join", PY, ALL) is None
    assert normalize_name("list.append", PY, ALL) is None
    assert normalize_name("main.helper", PY, ALL) == "main.helper"
    # The benchmark drops a user-prefixed builtin even when the user defined
    # it (`const fs = {...}`): both sides lose it alike.
    assert normalize_name("main.fs.readFileSync", JS, ALL) is None
    assert normalize_name("Array.prototype.map", JS, ALL) is None
    assert normalize_name("main.render", JS, ALL) == "main.render"
    assert normalize_name("java.util.List:add(java.lang.Object)", JAVA, ALL) is None
    assert normalize_name("String:length()", JAVA, ALL) is None
    # A dotted head is never a bare-class match: io.grpc.Key is not a JDK Key.
    assert normalize_name("io.grpc.Key:of()", JAVA, ALL) == "io.grpc.Key:of()"


def test_an_edge_goes_when_either_endpoint_does() -> None:
    edges = {("main", "print"), ("main", "main.Foo.__init__"), ("builtins.x", "main.f")}

    assert normalize_edges(edges, PY) == {("main", "main.Foo")}


def test_anonymous_names() -> None:
    assert is_anonymous("main.<lambda1>")
    assert is_anonymous("main.f.<genexpr>")
    assert is_anonymous("main.<arrow3>")
    assert is_anonymous("main.<arrow:handler>")
    assert not is_anonymous("main")
    assert not is_anonymous("pkg.Foo:<init>(java.lang.String)")


def test_see_through_follows_chains_and_drops_collapse_artifacts() -> None:
    edges = {
        ("main", "main.<arrow1>"),
        ("main.<arrow1>", "main.<arrow2>"),
        ("main.<arrow2>", "main.run"),
        ("main.f", "main.<lambda1>"),
        # f -> <lambda1> -> f would become a self-edge f -> f: dropped.
        ("main.<lambda1>", "main.f"),
        ("main.<lambda1>", "main.g"),
        ("main.f", "main.h"),
    }

    assert see_through_anonymous(edges) == {
        ("main", "main.run"),
        ("main.f", "main.g"),
        ("main.f", "main.h"),
    }


def test_class_bodies_are_not_calls(tmp_path: Path) -> None:
    source = (
        "class admin:\n"
        "    class AdminSite:\n"
        "        pass\n"
        "def factory():\n"
        "    class Local:\n"
        "        pass\n"
        "    return Local\n"
        "if True:\n"
        "    class Guarded:\n"
        "        pass\n"
    )
    (tmp_path / "main.py").write_text(source, encoding="utf-8")
    classes = python_class_names(tmp_path, [tmp_path / "main.py"])

    assert classes == {
        "main.admin",
        "main.admin.AdminSite",
        "main.factory.Local",
        "main.Guarded",
    }
    edges = {
        ("main", "main.admin"),
        ("main.admin", "main.admin.AdminSite"),
        # A call made in a class body belongs to the scope defining the class.
        ("main.admin.AdminSite", "main.helper"),
        ("main.factory", "main.factory.Local"),
        # A constructor is not a class body: it survives to the init mode.
        ("main", "main.admin.__init__"),
    }
    assert drop_class_bodies(edges, classes) == {
        ("main", "main.helper"),
        ("main", "main.admin.__init__"),
    }
