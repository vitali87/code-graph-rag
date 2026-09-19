"""`declared_parameters` for the eleven non-Python languages (issue #1804).

Each language's enumerator is proved on a real fixture against the three
things the Python review made explicit: declaration order rather than the
taint slot table, a variadic as ONE flagged slot, and the receiver excluded
by structure rather than by name. Every case below is one where the taint
slot table (`*_positional_parameter_slots`) would answer differently, so an
enumerator that delegated to it would go red here.

`index` is the declaration position among the slots a caller supplies. A
slot that binds no simple name (an unnamed C parameter, a destructuring
pattern, Rust `_`) keeps its position and yields no entry, so the indices of
the parameters after it still match the source.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.parameter_nodes import (
    DeclaredParameter,
    declared_parameters,
)


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _params(
    language: cs.SupportedLanguage,
    src: str,
    node_type: str,
    *,
    nth: int = 0,
    has_receiver: bool = True,
) -> list[DeclaredParameter]:
    parsers, _ = load_parsers()
    tree = parsers[language].parse(src.encode())
    nodes = [n for n in _walk(tree.root_node) if n.type == node_type]
    assert len(nodes) > nth, f"no {node_type!r} #{nth} in fixture"
    return declared_parameters(nodes[nth], language, has_receiver=has_receiver)


def _shape(
    got: list[DeclaredParameter],
) -> list[tuple[str, int, str | None, bool, bool]]:
    return [(p.name, p.index, p.type_name, p.is_variadic, p.has_default) for p in got]


# --- Go ----------------------------------------------------------------------


def test_go_grouped_names_each_take_a_slot_and_the_variadic_is_flagged() -> None:
    got = _params(
        cs.SupportedLanguage.GO,
        "package p\nfunc f(a, b int, c string, opts ...Opt) {}\n",
        "function_declaration",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "int", False, False),
        ("c", 2, "string", False, False),
        ("opts", 3, "...Opt", True, False),
    ]


def test_go_receiver_is_never_a_parameter_and_a_blank_slot_keeps_position() -> None:
    got = _params(
        cs.SupportedLanguage.GO,
        "package p\nfunc (r *Recv) m(x int, _ bool, y string) {}\n",
        "method_declaration",
    )
    assert _shape(got) == [
        ("x", 0, "int", False, False),
        ("y", 2, "string", False, False),
    ]
    # Go allows a list that is entirely unnamed; nothing binds, nothing emits.
    got = _params(
        cs.SupportedLanguage.GO,
        "package p\nfunc f(int, string) {}\n",
        "function_declaration",
    )
    assert got == []


# --- JavaScript / TypeScript -------------------------------------------------


def test_js_default_and_rest_parameters() -> None:
    got = _params(
        cs.SupportedLanguage.JS,
        "function f(a, b = 1, ...rest) {}\n",
        "function_declaration",
    )
    assert _shape(got) == [
        ("a", 0, None, False, False),
        ("b", 1, None, False, True),
        ("rest", 2, None, True, False),
    ]


def test_js_destructuring_slots_keep_their_position() -> None:
    got = _params(
        cs.SupportedLanguage.JS,
        "class C { m(x, {y, z}, [q], w) {} }\n",
        "method_definition",
    )
    assert [(p.name, p.index) for p in got] == [("x", 0), ("w", 3)]


def test_js_single_parameter_arrow_has_no_parameter_list() -> None:
    got = _params(cs.SupportedLanguage.JS, "const f = x => x;\n", "arrow_function")
    assert [(p.name, p.index) for p in got] == [("x", 0)]


def test_ts_this_parameter_takes_no_slot_and_annotations_are_read() -> None:
    got = _params(
        cs.SupportedLanguage.TS,
        "function f(this: Window, a: number, b?: string, c: number = 1,"
        " ...rest: string[]) {}\n",
        "function_declaration",
    )
    assert _shape(got) == [
        ("a", 0, "number", False, False),
        ("b", 1, "string", False, False),
        ("c", 2, "number", False, True),
        ("rest", 3, "string[]", True, False),
    ]


def test_ts_parameter_property_and_untyped_default() -> None:
    got = _params(
        cs.SupportedLanguage.TS,
        "class C { constructor(public y: string, z = 2) {} }\n",
        "method_definition",
    )
    assert _shape(got) == [
        ("y", 0, "string", False, False),
        ("z", 1, None, False, True),
    ]


def test_tsx_uses_the_typescript_reader() -> None:
    got = _params(
        cs.SupportedLanguage.TSX,
        "function f(a: number) {}\n",
        "function_declaration",
    )
    assert _shape(got) == [("a", 0, "number", False, False)]


# --- C++ ---------------------------------------------------------------------


def test_cpp_default_unnamed_and_c_style_variadic() -> None:
    got = _params(
        cs.SupportedLanguage.CPP,
        'int f(int a, const std::string& b = "x", int, ...) { return 0; }\n',
        "function_definition",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "std::string", False, True),
    ]


def test_cpp_out_of_class_definition_and_pointer_declarator() -> None:
    got = _params(
        cs.SupportedLanguage.CPP,
        "void C::m(int x, char* y) {}\n",
        "function_definition",
    )
    assert _shape(got) == [
        ("x", 0, "int", False, False),
        ("y", 1, "char", False, False),
    ]


def test_cpp_parameter_pack_is_one_variadic_slot() -> None:
    got = _params(
        cs.SupportedLanguage.CPP,
        "template<typename... Args> void v(Args&&... args) {}\n",
        "template_declaration",
    )
    assert _shape(got) == [("args", 0, "Args", True, False)]


def test_cpp_lambda_and_prototype_declaration() -> None:
    got = _params(
        cs.SupportedLanguage.CPP,
        "auto l = [](int a, auto b) { return a; };\n",
        "lambda_expression",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "auto", False, False),
    ]
    got = _params(cs.SupportedLanguage.CPP, "void p(int a, char);\n", "declaration")
    assert _shape(got) == [("a", 0, "int", False, False)]


# --- Java --------------------------------------------------------------------


def test_java_varargs_and_receiver_parameter() -> None:
    got = _params(
        cs.SupportedLanguage.JAVA,
        "class C { void m(final int a, String... xs) {} }\n",
        "method_declaration",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("xs", 1, "String...", True, False),
    ]
    got = _params(
        cs.SupportedLanguage.JAVA,
        "class C { void r(C this, int b) {} }\n",
        "method_declaration",
    )
    assert _shape(got) == [("b", 0, "int", False, False)]


def test_java_varargs_type_survives_a_modifier_or_annotation() -> None:
    """`final String... xs`: the spread's first named child is `modifiers`,
    which is not the element type (local review P1)."""
    got = _params(
        cs.SupportedLanguage.JAVA,
        "class C { void m(final String... xs, @NonNull List<String>... ys) {} }\n",
        "method_declaration",
    )
    assert [(p.name, p.type_name, p.is_variadic) for p in got] == [
        ("xs", "String...", True),
        ("ys", "List<String>...", True),
    ]


def test_java_constructor_generic_type_text() -> None:
    got = _params(
        cs.SupportedLanguage.JAVA,
        "class C { C(int a, java.util.List<String> b) {} }\n",
        "constructor_declaration",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "java.util.List<String>", False, False),
    ]


# --- C# ----------------------------------------------------------------------


def test_csharp_modifiers_default_and_params_array() -> None:
    got = _params(
        cs.SupportedLanguage.CSHARP,
        "class C { void M(int a, ref string b, out int c, int d = 2,"
        " params int[] rest) {} }\n",
        "method_declaration",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "string", False, False),
        ("c", 2, "int", False, False),
        ("d", 3, "int", False, True),
        ("rest", 4, "params int[]", True, False),
    ]


def test_csharp_extension_receiver_is_a_declared_parameter() -> None:
    """`this C self` IS caller-supplied (`c.E(x)` or `E(c, x)`), unlike a
    Python `self`, so it keeps its slot."""
    got = _params(
        cs.SupportedLanguage.CSHARP,
        "static class X { static void E(this C self, int x) {} }\n",
        "method_declaration",
    )
    assert _shape(got) == [
        ("self", 0, "C", False, False),
        ("x", 1, "int", False, False),
    ]


def test_csharp_constructor_and_local_function() -> None:
    got = _params(
        cs.SupportedLanguage.CSHARP,
        'class C { C(int a, string b = "x") {} }\n',
        "constructor_declaration",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "string", False, True),
    ]
    got = _params(
        cs.SupportedLanguage.CSHARP,
        "class C { void M() { int L(int q) => q; } }\n",
        "local_function_statement",
    )
    assert _shape(got) == [("q", 0, "int", False, False)]


# --- Lua ---------------------------------------------------------------------


def test_lua_vararg_binds_nothing_and_colon_receiver_is_implicit() -> None:
    """Lua requires `...` to come last, so whether it keeps or drops its
    position is unobservable from the entries; no mutation of that branch
    reddens this test, and it is not evidence about position-keeping."""
    got = _params(
        cs.SupportedLanguage.LUA,
        "function f(a, b, ...) end\n",
        "function_declaration",
    )
    assert _shape(got) == [
        ("a", 0, None, False, False),
        ("b", 1, None, False, False),
    ]
    got = _params(
        cs.SupportedLanguage.LUA,
        "function obj:m(x, y) end\n",
        "function_declaration",
    )
    assert [(p.name, p.index) for p in got] == [("x", 0), ("y", 1)]
    got = _params(
        cs.SupportedLanguage.LUA,
        "local g = function(p, q) end\n",
        "function_definition",
    )
    assert [(p.name, p.index) for p in got] == [("p", 0), ("q", 1)]


# --- Scala -------------------------------------------------------------------


def test_scala_default_and_repeated_parameter() -> None:
    got = _params(
        cs.SupportedLanguage.SCALA,
        'object O { def f(a: Int, b: String = "x", xs: Int*): Unit = {} }\n',
        "function_definition",
    )
    assert _shape(got) == [
        ("a", 0, "Int", False, False),
        ("b", 1, "String", False, True),
        ("xs", 2, "Int*", True, False),
    ]


def test_scala_every_curried_list_is_declared_in_order() -> None:
    """The taint table reads only the first list; a declaration has them all."""
    got = _params(
        cs.SupportedLanguage.SCALA,
        "object O { def c(a: Int)(b: Int)(implicit ctx: Ctx): Unit = {} }\n",
        "function_definition",
    )
    assert _shape(got) == [
        ("a", 0, "Int", False, False),
        ("b", 1, "Int", False, False),
        ("ctx", 2, "Ctx", False, False),
    ]


# --- Rust --------------------------------------------------------------------


def test_rust_patterns_are_unwrapped_and_unbound_slots_keep_position() -> None:
    got = _params(
        cs.SupportedLanguage.RUST,
        "fn f(a: i32, (b, c): (i32, i32), &mut d: &mut i32, ref e: E, _: u8,"
        " mut g: G) {}\n",
        "function_item",
    )
    assert _shape(got) == [
        ("a", 0, "i32", False, False),
        ("d", 2, "&mut i32", False, False),
        ("e", 3, "E", False, False),
        ("g", 5, "G", False, False),
    ]


def test_rust_self_in_either_form_takes_no_slot() -> None:
    src = "impl S { fn m(&self, x: i32) {} fn n(self: Box<Self>, y: i32) {} }\n"
    assert _shape(_params(cs.SupportedLanguage.RUST, src, "function_item")) == [
        ("x", 0, "i32", False, False)
    ]
    assert _shape(_params(cs.SupportedLanguage.RUST, src, "function_item", nth=1)) == [
        ("y", 0, "i32", False, False)
    ]


def test_rust_closure_parameters_may_be_bare_identifiers() -> None:
    got = _params(
        cs.SupportedLanguage.RUST,
        "fn f() { let c = |a: i32, b| a; }\n",
        "closure_expression",
    )
    assert _shape(got) == [
        ("a", 0, "i32", False, False),
        ("b", 1, None, False, False),
    ]


def test_rust_closure_patterns_and_blank_keep_their_positions() -> None:
    """In a closure the patterns are bare and `_` is an anonymous token, so
    a walk over named children alone drops `x` and mis-indexes `z` (local
    review P1)."""
    got = _params(
        cs.SupportedLanguage.RUST,
        "fn f() { let c = |(a, b), &x, mut y, _, z| z; }\n",
        "closure_expression",
    )
    assert [(p.name, p.index) for p in got] == [("x", 1), ("y", 2), ("z", 4)]


# --- C -----------------------------------------------------------------------


def test_c_unnamed_and_variadic_slots_keep_position() -> None:
    got = _params(
        cs.SupportedLanguage.C,
        "int f(int a, const char *b, int, ..., int z) { return 0; }\n",
        "function_definition",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "char", False, False),
        ("z", 4, "int", False, False),
    ]


def test_c_function_pointer_and_array_declarators_are_unwrapped() -> None:
    got = _params(
        cs.SupportedLanguage.C,
        "static int h(int (*cb)(int), int arr[]) { return 0; }\n",
        "function_definition",
    )
    assert _shape(got) == [
        ("cb", 0, "int", False, False),
        ("arr", 1, "int", False, False),
    ]
    assert (
        _params(cs.SupportedLanguage.C, "void g(void) {}\n", "function_definition")
        == []
    )


# --- PHP ---------------------------------------------------------------------


def test_php_names_drop_the_sigil_and_variadic_by_ref_defaults_are_read() -> None:
    got = _params(
        cs.SupportedLanguage.PHP,
        "<?php\nfunction f(int $a, ?string $b = null, int ...$rest) {}\n"
        "function g(int &$byref = 1) {}\n",
        "function_definition",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "?string", False, True),
        ("rest", 2, "int", True, False),
    ]
    got = _params(
        cs.SupportedLanguage.PHP,
        "<?php\nfunction g(int &$byref = 1) {}\n",
        "function_definition",
    )
    assert _shape(got) == [("byref", 0, "int", False, True)]


def test_php_promoted_constructor_properties_are_parameters() -> None:
    got = _params(
        cs.SupportedLanguage.PHP,
        "<?php\nclass C { function __construct(private int $x,"
        ' public readonly string $y = "a") {} }\n',
        "method_declaration",
    )
    assert _shape(got) == [
        ("x", 0, "int", False, False),
        ("y", 1, "string", False, True),
    ]


def test_php_arrow_and_anonymous_functions() -> None:
    got = _params(
        cs.SupportedLanguage.PHP, "<?php\n$f = fn(int $x) => $x;\n", "arrow_function"
    )
    assert _shape(got) == [("x", 0, "int", False, False)]
    got = _params(
        cs.SupportedLanguage.PHP,
        "<?php\n$g = function($a, $b = 1) use ($c) {};\n",
        "anonymous_function",
    )
    assert _shape(got) == [
        ("a", 0, None, False, False),
        ("b", 1, None, False, True),
    ]


# --- Dart --------------------------------------------------------------------


def test_dart_optional_positional_parameters_and_defaults() -> None:
    got = _params(
        cs.SupportedLanguage.DART,
        "void f(int a, [String b = 'x', int? c]) {}\n",
        "function_signature",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "String", False, True),
        ("c", 2, "int?", False, False),
    ]


def test_dart_named_parameters_are_declared_in_order() -> None:
    got = _params(
        cs.SupportedLanguage.DART,
        "void g(int a, {required int b, String c = 'd'}) {}\n",
        "function_signature",
    )
    assert _shape(got) == [
        ("a", 0, "int", False, False),
        ("b", 1, "int", False, False),
        ("c", 2, "String", False, True),
    ]


def test_dart_initialising_formals_and_generic_type_text() -> None:
    got = _params(
        cs.SupportedLanguage.DART,
        "class C { C(this.x, {this.y}); int x; int? y; }\n",
        "constructor_signature",
    )
    assert _shape(got) == [
        ("x", 0, None, False, False),
        ("y", 1, None, False, False),
    ]
    got = _params(
        cs.SupportedLanguage.DART,
        "void main(List<String> args, final int n) {}\n",
        "function_signature",
    )
    assert _shape(got) == [
        ("args", 0, "List<String>", False, False),
        ("n", 1, "int", False, False),
    ]


def test_dart_method_and_setter_signatures() -> None:
    src = "class C { void m(int y) {} set v(int x) {} }\n"
    assert _shape(_params(cs.SupportedLanguage.DART, src, "function_signature")) == [
        ("y", 0, "int", False, False)
    ]
    assert _shape(_params(cs.SupportedLanguage.DART, src, "setter_signature")) == [
        ("x", 0, "int", False, False)
    ]


def test_dart_annotation_is_not_part_of_the_type() -> None:
    """`@Deprecated('x') int b` (local review P1); a `final` after the
    annotation is dropped too."""
    got = _params(
        cs.SupportedLanguage.DART,
        "void f({@Deprecated('x') int b, @required final String? c}) {}\n",
        "function_signature",
    )
    assert [(p.name, p.type_name) for p in got] == [("b", "int"), ("c", "String?")]


def test_dart_super_initialising_formal_is_a_parameter() -> None:
    got = _params(
        cs.SupportedLanguage.DART,
        "class C extends B { C(super.x, this.y); }\n",
        "constructor_signature",
    )
    assert [(p.name, p.index, p.type_name) for p in got] == [
        ("x", 0, None),
        ("y", 1, None),
    ]


def test_dart_old_style_function_typed_parameter_is_named() -> None:
    """`void cb(int i)` has no `name` field; its own parameter list is not
    a type expression, so it carries no type."""
    got = _params(
        cs.SupportedLanguage.DART,
        "void f(void cb(int i), String Function(int) g) {}\n",
        "function_signature",
    )
    assert [(p.name, p.index, p.type_name) for p in got] == [
        ("cb", 0, None),
        ("g", 1, "String Function(int)"),
    ]


def test_dart_colon_default_is_a_default() -> None:
    got = _params(
        cs.SupportedLanguage.DART,
        "void f({int b: 1, int c}) {}\n",
        "function_signature",
    )
    assert [(p.name, p.has_default) for p in got] == [("b", True), ("c", False)]


# --- Dispatch ----------------------------------------------------------------


@pytest.mark.parametrize("language", [None, cs.SupportedLanguage.SQL])
def test_an_uncovered_language_declares_nothing(language) -> None:
    parsers, _ = load_parsers()
    tree = parsers[cs.SupportedLanguage.GO].parse(b"package p\nfunc f(a int) {}\n")
    fn = next(n for n in _walk(tree.root_node) if n.type == "function_declaration")
    assert declared_parameters(fn, language, has_receiver=False) == []


def test_position_is_the_name_node_one_based_line() -> None:
    got = _params(
        cs.SupportedLanguage.GO,
        "package p\nfunc f(\n\ta int,\n\tb string,\n) {}\n",
        "function_declaration",
    )
    assert [(p.start_line, p.start_col) for p in got] == [(3, 1), (4, 1)]
