"""`class_ingest.identity`: the (qn, name, exported) a class node registers
under, through the language's FQN spec and through the fallback that names
a class the spec cannot (a C++ `export class` mis-parse, a class read
without its file), and the nested-module prefix of a Rust type."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parsers.class_ingest.identity import (
    build_nested_qualified_name_for_class,
    extract_class_name,
    extract_cpp_class_name,
    resolve_class_identity,
    resolve_class_identity_fallback,
)
from codebase_rag.tests.collaborator_support import first, nodes_of, parse, walk

_L = cs.SupportedLanguage


def _identity(
    node: Node, language: cs.SupportedLanguage, file_name: str | None
) -> tuple[str, str, bool] | None:
    file_path = Path("/repo", file_name) if file_name is not None else None
    return resolve_class_identity(
        node, "proj.m", language, LANGUAGE_SPECS[language], file_path
    )


def test_a_nested_python_class_is_named_under_its_enclosing_class() -> None:
    root = parse(_L.PYTHON, "class Outer:\n    class Inner: ...\nclass _Hidden: ...\n")
    outer, inner, hidden = nodes_of(root, "class_definition")
    assert _identity(outer, _L.PYTHON, "m.py") == ("proj.m.Outer", "Outer", True)
    assert _identity(inner, _L.PYTHON, "m.py") == ("proj.m.Outer.Inner", "Inner", True)
    # A leading underscore is Python's "not exported".
    assert _identity(hidden, _L.PYTHON, "m.py") == ("proj.m._Hidden", "_Hidden", False)


def test_without_its_file_a_class_is_named_by_the_fallback_alike() -> None:
    root = parse(_L.PYTHON, "class Outer:\n    class Inner: ...\n")
    for node in nodes_of(root, "class_definition"):
        assert _identity(node, _L.PYTHON, None) == _identity(node, _L.PYTHON, "m.py")


def test_a_js_class_expression_takes_the_name_of_its_variable() -> None:
    root = parse(_L.JS, "const Foo = class { m() {} };\nexport class Bar {}\n")
    (expression,) = [n for n in walk(root) if n.type == "class" and n.is_named]
    assert extract_class_name(expression) == "Foo"
    assert _identity(expression, _L.JS, "m.js") == ("proj.m.Foo", "Foo", False)
    declaration = first(root, "class_declaration")
    assert _identity(declaration, _L.JS, "m.js") == ("proj.m.Bar", "Bar", True)


def test_a_class_with_no_name_and_no_variable_gets_no_identity() -> None:
    root = parse(_L.JS, "x = class {};\n")
    (expression,) = [n for n in walk(root) if n.type == "class" and n.is_named]
    assert extract_class_name(expression) is None
    assert (
        resolve_class_identity_fallback(
            expression, "proj.m", _L.JS, LANGUAGE_SPECS[_L.JS]
        )
        is None
    )


def test_a_cpp_template_is_named_after_the_class_it_wraps() -> None:
    root = parse(_L.CPP, "namespace ns { template <typename T> class Box { T t; }; }\n")
    template = first(root, "template_declaration")
    assert extract_cpp_class_name(template) == "Box"
    assert resolve_class_identity_fallback(
        template, "proj.m", _L.CPP, LANGUAGE_SPECS[_L.CPP]
    ) == ("proj.m.ns.Box", "Box", False)


def test_an_anonymous_cpp_class_gets_no_identity() -> None:
    root = parse(_L.CPP, "class { int x; } anon;\n")
    specifier = first(root, "class_specifier")
    assert _identity(specifier, _L.CPP, "m.cpp") is None


def test_an_exported_cpp_module_class_is_named_from_its_mis_parse() -> None:
    # The grammar reads `export` as a type and `class` as the declarator,
    # where the FQN spec would take the keyword for the class's name.
    root = parse(
        _L.CPP, "export module a;\nexport class Shape { public: int area(); };\n"
    )
    misparse = first(root, "function_definition")
    assert misparse.type in cs.CPP_EXPORTED_CLASS_PARSE_TYPES
    assert _identity(misparse, _L.CPP, "a.cppm") == ("proj.m.Shape", "Shape", True)


def test_a_rust_type_in_inline_modules_is_prefixed_with_them() -> None:
    root = parse(
        _L.RUST, "mod outer { pub mod inner { pub struct S; } }\nstruct Top;\n"
    )
    nested, top = nodes_of(root, "struct_item")
    spec = LANGUAGE_SPECS[_L.RUST]
    assert (
        build_nested_qualified_name_for_class(nested, "proj.m", "S", spec)
        == "proj.m.outer.inner.S"
    )
    # At file scope there is no prefix to add; the caller uses module.name.
    assert build_nested_qualified_name_for_class(top, "proj.m", "Top", spec) is None
    assert resolve_class_identity_fallback(top, "proj.m", _L.RUST, spec) == (
        "proj.m.Top",
        "Top",
        False,
    )
