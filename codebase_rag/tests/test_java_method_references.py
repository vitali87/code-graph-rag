# A Java method reference (`Type::m`, `obj::m`, `this::m`, `super::m`,
# `Type::new`) hands a method to someone else as a value, the way a C# method
# group or a Python callback argument does. It produced no edge at all, so a
# method used only through one (a collector's accumulator, a stream stage, a
# comparator) looked unreachable and `cgr dead-code` reported it (issue #2546).
# It is recorded as REFERENCES, the relationship every other language uses for
# a function passed as a value; `Type::new` also INSTANTIATES the type.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import PropertyDict
from evals.dead_code import cgr_dead_code, default_dead_code_config

_JOINER = (
    "import java.util.List;\n"
    "import java.util.function.Function;\n"
    "import java.util.stream.Collector;\n"
    "import java.util.stream.Collectors;\n"
    "\n"
    "public final class Joiner {\n"
    "  static final class Acc {\n"
    "    private final StringBuilder sb = new StringBuilder();\n"
    "    Acc add(String s) { sb.append(s); return this; }\n"
    "    Acc merge(Acc o) { sb.append(o.sb); return this; }\n"
    "    String finish() { return sb.toString(); }\n"
    "  }\n"
    "\n"
    "  static String upper(String s) { return s.toUpperCase(); }\n"
    "\n"
    "  private static void unused() { }\n"
    "\n"
    "  public static Collector<String, Acc, String> joining() {\n"
    "    return Collector.of(Acc::new, Acc::add, Acc::merge, Acc::finish);\n"
    "  }\n"
    "\n"
    "  public static List<String> shout(List<String> xs) {\n"
    "    Function<String, String> f = Joiner::upper;\n"
    "    return xs.stream().map(f).collect(Collectors.toList());\n"
    "  }\n"
    "}\n"
)


def _write(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "com" / "acme"
    pkg.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (pkg / name).write_text(f"package com.acme;\n\n{body}", encoding="utf-8")
    return root


def _index(
    temp_repo: Path, mock_ingestor, files: dict[str, str]
) -> list[tuple[str, str, str, PropertyDict]]:
    _write(temp_repo, files)
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    edges: list[tuple[str, str, str, PropertyDict]] = []
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        edges.append((str(c.args[1]), str(c.args[0][2]), str(c.args[2][2]), props))
    return edges


def _targets(
    edges: list[tuple[str, str, str, PropertyDict]], rel: str, caller_suffix: str
) -> dict[str, str]:
    # target qn -> resolution label, for one relationship out of one caller.
    return {
        dst: str(props.get(cs.KEY_RESOLUTION))
        for rel_type, src, dst, props in edges
        if rel_type == rel and src.endswith(caller_suffix)
    }


def _ends(targets: dict[str, str], suffix: str) -> list[str]:
    return [t for t in targets if t.endswith(suffix)]


def _all_outgoing(
    edges: list[tuple[str, str, str, PropertyDict]], caller_suffix: str
) -> set[tuple[str, str]]:
    call_rels = {
        cs.RelationshipType.CALLS,
        cs.RelationshipType.REFERENCES,
        cs.RelationshipType.INSTANTIATES,
    }
    return {
        (rel_type, dst)
        for rel_type, src, dst, _ in edges
        if rel_type in call_rels and src.endswith(caller_suffix)
    }


# --- the forms of a method reference ---------------------------------------------


def test_static_method_reference_references_the_method(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(temp_repo, mock_ingestor, {"Joiner.java": _JOINER})
    refs = _targets(edges, "REFERENCES", ".Joiner.shout(List<String>)")
    assert _ends(refs, ".Joiner.upper(String)"), refs
    assert refs[_ends(refs, ".Joiner.upper(String)")[0]] == cs.EdgeResolution.EXACT
    # A reference hands the method over; it does not invoke it here.
    calls = _targets(edges, "CALLS", ".Joiner.shout(List<String>)")
    assert not _ends(calls, ".upper(String)"), calls


def test_unbound_receiver_references_the_instance_methods(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(temp_repo, mock_ingestor, {"Joiner.java": _JOINER})
    refs = _targets(edges, "REFERENCES", ".Joiner.joining()")
    for member in (".Acc.add(String)", ".Acc.merge(Acc)", ".Acc.finish()"):
        assert _ends(refs, member), (member, refs)


def test_bound_receiver_references_the_receivers_method(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Sink.java": "public class Sink {\n  void accept(String s) { }\n}\n",
            "User.java": (
                "import java.util.List;\n"
                "public class User {\n"
                "  private final Sink field = new Sink();\n"
                "  void viaParam(List<String> xs, Sink sink) { xs.forEach(sink::accept); }\n"
                "  void viaLocal(List<String> xs) {\n"
                "    Sink local = new Sink();\n"
                "    xs.forEach(local::accept);\n"
                "  }\n"
                "  void viaField(List<String> xs) { xs.forEach(this.field::accept); }\n"
                "}\n"
            ),
        },
    )
    for caller in (
        ".User.viaParam(List<String>,Sink)",
        ".User.viaLocal(List<String>)",
        ".User.viaField(List<String>)",
    ):
        refs = _targets(edges, "REFERENCES", caller)
        assert _ends(refs, ".Sink.accept(String)"), (caller, refs)


def test_this_and_super_references_bind_the_right_class(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Base.java": (
                "public class Base {\n  String fmt(String s) { return s; }\n}\n"
            ),
            "Child.java": (
                "import java.util.List;\n"
                "public class Child extends Base {\n"
                '  String fmt(String s) { return s + "!"; }\n'
                "  void mine(List<String> xs) { xs.stream().map(this::fmt); }\n"
                "  void parent(List<String> xs) { xs.stream().map(super::fmt); }\n"
                "}\n"
            ),
        },
    )
    mine = _targets(edges, "REFERENCES", ".Child.mine(List<String>)")
    assert _ends(mine, ".Child.fmt(String)"), mine
    assert not _ends(mine, ".Base.fmt(String)"), mine
    parent = _targets(edges, "REFERENCES", ".Child.parent(List<String>)")
    assert _ends(parent, ".Base.fmt(String)"), parent
    # `super::fmt` names the parent's method, never the override beside it.
    assert not _ends(parent, ".Child.fmt(String)"), parent


def test_constructor_reference_instantiates_and_references_every_constructor(
    temp_repo: Path, mock_ingestor
) -> None:
    # The functional interface that would pick one constructor is not known to
    # the parser, so every declared constructor is referenced, as `overload`.
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Box.java": "public class Box {\n  Box() { }\n  Box(int n) { }\n}\n",
            "User.java": (
                "import java.util.function.Supplier;\n"
                "public class User {\n"
                "  Supplier<Box> make() { return Box::new; }\n"
                "}\n"
            ),
        },
    )
    insts = _targets(edges, "INSTANTIATES", ".User.make()")
    assert _ends(insts, ".Box"), insts
    refs = _targets(edges, "REFERENCES", ".User.make()")
    ctors = {t: r for t, r in refs.items() if t.rsplit(".", 1)[-1].startswith("Box(")}
    assert {t.rsplit(".", 1)[-1] for t in ctors} == {"Box()", "Box(int)"}, refs
    assert set(ctors.values()) == {cs.EdgeResolution.OVERLOAD}, ctors
    # No constructor runs at the reference site.
    assert not _targets(edges, "CALLS", ".User.make()"), edges


def test_generic_and_nested_constructor_references(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Outer.java": (
                "public class Outer {\n"
                "  static class Inner { Inner() { } }\n"
                "  static class Cell<T> { Cell() { } }\n"
                "}\n"
            ),
            "User.java": (
                "import java.util.function.Supplier;\n"
                "public class User {\n"
                "  Supplier<Outer.Inner> nested() { return Outer.Inner::new; }\n"
                "  Supplier<Outer.Cell<String>> generic() {\n"
                "    return Outer.Cell<String>::new;\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _ends(_targets(edges, "INSTANTIATES", ".User.nested()"), ".Outer.Inner")
    assert _ends(
        _targets(edges, "REFERENCES", ".User.nested()"), ".Outer.Inner.Inner()"
    )
    assert _ends(_targets(edges, "INSTANTIATES", ".User.generic()"), ".Outer.Cell")
    assert _ends(_targets(edges, "REFERENCES", ".User.generic()"), ".Outer.Cell.Cell()")


def test_expression_receivers_are_typed_like_call_receivers(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Svc.java": (
                "import java.util.List;\n"
                "public class Svc {\n"
                "  static Svc make() { return new Svc(); }\n"
                "  static int helper(String s) { return 1; }\n"
                "  int inst(String s) { return 2; }\n"
                "  void chained(List<String> xs) { xs.stream().map(make()::inst); }\n"
                "  void created(List<String> xs) { xs.stream().map(new Svc()::inst); }\n"
                "  void cast(Object o, List<String> xs) {\n"
                "    xs.stream().map(((Svc) o)::inst);\n"
                "  }\n"
                "  void typeArgs(List<String> xs) {\n"
                "    xs.stream().map(Svc::<String>helper);\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    for caller, target in (
        (".Svc.chained(List<String>)", ".Svc.inst(String)"),
        (".Svc.created(List<String>)", ".Svc.inst(String)"),
        (".Svc.cast(Object,List<String>)", ".Svc.inst(String)"),
        (".Svc.typeArgs(List<String>)", ".Svc.helper(String)"),
    ):
        assert _ends(_targets(edges, "REFERENCES", caller), target), (caller, edges)


def test_reference_is_owned_by_the_scope_a_call_there_would_be(
    temp_repo: Path, mock_ingestor
) -> None:
    # A lambda is no caller of its own, so its reference belongs to the method
    # around it; an anonymous class's method is one, and keeps its reference.
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Own.java": (
                "import java.util.List;\n"
                "import java.util.function.Function;\n"
                "public class Own {\n"
                "  static int a(String s) { return 1; }\n"
                "  static int b(String s) { return 2; }\n"
                "  void lambda(List<String> xs) {\n"
                "    xs.forEach(x -> xs.stream().map(Own::a));\n"
                "  }\n"
                "  Runnable anon() {\n"
                "    return new Runnable() {\n"
                "      public void run() { Function<String, Integer> f = Own::b; }\n"
                "    };\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _ends(
        _targets(edges, "REFERENCES", ".Own.lambda(List<String>)"), ".a(String)"
    )
    assert _ends(_targets(edges, "REFERENCES", ".Own.anon.run"), ".Own.b(String)")
    assert not _ends(_targets(edges, "REFERENCES", ".Own.anon()"), ".Own.b(String)")


def test_overloaded_method_reference_references_each_overload(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Fmt.java": (
                "public class Fmt {\n"
                '  static String of(int n) { return ""; }\n'
                "  static String of(String s) { return s; }\n"
                "}\n"
            ),
            "User.java": (
                "import java.util.function.Function;\n"
                "public class User {\n"
                "  Function<String, String> pick() { return Fmt::of; }\n"
                "}\n"
            ),
        },
    )
    refs = _targets(edges, "REFERENCES", ".User.pick()")
    overloads = {t: r for t, r in refs.items() if ".Fmt.of(" in t}
    assert {t.rsplit(".", 1)[-1] for t in overloads} == {"of(int)", "of(String)"}
    assert set(overloads.values()) == {cs.EdgeResolution.OVERLOAD}, overloads


def test_inherited_overloads_are_referenced_but_overridden_ones_are_not(
    temp_repo: Path, mock_ingestor
) -> None:
    # `Leaf::of` may denote the overload `Leaf` declares or one it inherits;
    # the lookup stops at the first class declaring any `of`, so the parent's
    # other overload must still be referenced. An overridden parent overload
    # is not what the reference denotes and stays unreferenced.
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Root.java": (
                'public class Root {\n  String of(long n) { return ""; }\n}\n'
            ),
            "Base.java": (
                "public class Base extends Root {\n"
                '  String of(int n) { return ""; }\n'
                "  String of(String s) { return s; }\n"
                "}\n"
            ),
            "Leaf.java": (
                "public class Leaf extends Base {\n"
                '  @Override String of(int n) { return "leaf"; }\n'
                "}\n"
            ),
            "User.java": (
                "import java.util.function.BiFunction;\n"
                "public class User {\n"
                "  BiFunction<Leaf, String, String> pick() { return Leaf::of; }\n"
                "}\n"
            ),
        },
    )
    refs = _targets(edges, "REFERENCES", ".User.pick()")
    overloads = {t.rsplit(".", 2)[-2] + "." + t.rsplit(".", 1)[-1] for t in refs}
    assert overloads == {"Leaf.of(int)", "Base.of(String)", "Root.of(long)"}, refs
    assert set(refs.values()) == {cs.EdgeResolution.OVERLOAD}, refs


def test_field_initializer_method_reference_is_referenced_from_the_module(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Table.java": (
                "import java.util.function.Function;\n"
                "public class Table {\n"
                "  static final Function<String, String> F = Table::trim;\n"
                "  static String trim(String s) { return s.trim(); }\n"
                "}\n"
            ),
        },
    )
    refs = {
        dst
        for rel, _, dst, _ in edges
        if rel == cs.RelationshipType.REFERENCES and dst.endswith(".trim(String)")
    }
    assert refs, edges


def test_method_used_only_through_a_reference_is_not_dead(tmp_path: Path) -> None:
    root = _write(tmp_path / "jref", {"Joiner.java": _JOINER})
    dead = cgr_dead_code(root, "jref", default_dead_code_config(False, False))
    for member in (
        ".Acc.add(String)",
        ".Acc.merge(Acc)",
        ".Acc.finish()",
        ".Joiner.upper(String)",
    ):
        assert not [d for d in dead if d.endswith(member)], (member, sorted(dead))
    # A private method nothing refers to is still dead.
    assert [d for d in dead if d.endswith(".Joiner.unused()")], sorted(dead)


# --- what must not change ----------------------------------------------------------


@pytest.mark.parametrize("ref", ["int[]::new", "Acc[]::new"])
def test_array_constructor_reference_emits_no_edge(
    temp_repo: Path, mock_ingestor, ref: str
) -> None:
    # An array constructor reference builds an array, never an Acc.
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Acc.java": "public class Acc {\n  Acc() { }\n}\n",
            "User.java": (
                "import java.util.function.IntFunction;\n"
                "public class User {\n"
                f"  Object make() {{ IntFunction<Object> f = {ref}; return f; }}\n"
                "}\n"
            ),
        },
    )
    assert not _all_outgoing(edges, ".User.make()"), edges


def test_jdk_method_references_stay_external(temp_repo: Path, mock_ingestor) -> None:
    # First-party methods sharing a JDK method's name must not capture it.
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Printer.java": (
                "public class Printer {\n"
                "  void println(String s) { }\n"
                '  static String valueOf(Object o) { return ""; }\n'
                "  void accept(String s) { }\n"
                "}\n"
            ),
            "User.java": (
                "import java.util.List;\n"
                "import java.util.function.Consumer;\n"
                "public class User {\n"
                "  void run(List<String> xs, Consumer<String> c) {\n"
                "    xs.forEach(System.out::println);\n"
                "    xs.stream().map(String::valueOf);\n"
                "    xs.forEach(c::accept);\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    outgoing = _all_outgoing(edges, ".User.run(List<String>,Consumer<String>)")
    assert not [dst for _, dst in outgoing if ".Printer." in dst], outgoing


def test_lambda_keeps_its_call_edge(temp_repo: Path, mock_ingestor) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "User.java": (
                "import java.util.List;\n"
                "public class User {\n"
                "  static String foo(String s) { return s; }\n"
                "  void run(List<String> xs) { xs.stream().map(x -> foo(x)); }\n"
                "}\n"
            ),
        },
    )
    assert _ends(_targets(edges, "CALLS", ".User.run(List<String>)"), ".foo(String)")
    assert not _ends(
        _targets(edges, "REFERENCES", ".User.run(List<String>)"), ".foo(String)"
    )


def test_field_access_is_not_a_method_reference(temp_repo: Path, mock_ingestor) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Holder.java": (
                "public class Holder {\n  int b;\n  int b() { return 1; }\n}\n"
            ),
            "User.java": (
                "public class User {\n  int read(Holder a) { return a.b; }\n}\n"
            ),
        },
    )
    assert not _all_outgoing(edges, ".User.read(Holder)"), edges


def test_same_named_method_in_an_unrelated_class_is_not_bound(
    temp_repo: Path, mock_ingestor
) -> None:
    edges = _index(
        temp_repo,
        mock_ingestor,
        {
            "Acc.java": "public class Acc {\n  Acc add(String s) { return this; }\n}\n",
            "Other.java": (
                "public class Other {\n  Other add(String s) { return this; }\n}\n"
            ),
            "User.java": (
                "import java.util.function.BiFunction;\n"
                "public class User {\n"
                "  BiFunction<Acc, String, Acc> pick() { return Acc::add; }\n"
                "}\n"
            ),
        },
    )
    refs = _targets(edges, "REFERENCES", ".User.pick()")
    assert _ends(refs, ".Acc.add(String)"), refs
    assert not _ends(refs, ".Other.add(String)"), refs
