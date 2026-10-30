"""The JS/TS binding indexes the receiver resolution reads: every name a file
introduces and where, the introductions one scope owns with the spans they
govern, which of them decides a read, and the function each name declares."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.js_ts.bindings import (
    DECL_DESTRUCTURED,
    DECL_PARAM,
    DECL_PLAIN,
    JsFileBindingCollector,
    innermost_enclosing,
    named_functions,
    scope_introductions,
)
from codebase_rag.parsers.js_ts.calls import unwrap_ts_value
from codebase_rag.tests.collaborator_support import first, parse

_JS = cs.SupportedLanguage.JS


def _root(source: str, language: cs.SupportedLanguage = _JS) -> Node:
    return parse(language, source)


def test_a_scope_owns_its_params_and_declarations_but_not_nested_ones() -> None:
    root = _root(
        "function f(a, { b, c: d } = {}, ...rest) {\n"
        "  const x = 1;\n  let { y } = obj;\n  var z;\n"
        "  function inner() { const hidden = 2; }\n}\n"
    )
    intros = scope_introductions(first(root, "function_declaration"))
    # `c` is a key, not a binding; `inner`'s locals are its own.
    assert set(intros) == {"a", "b", "d", "rest", "x", "y", "z"}
    assert intros["a"][0][2] == DECL_PARAM
    assert intros["x"][0][2] == DECL_PLAIN
    assert intros["y"][0][2] == DECL_DESTRUCTURED
    # `var z;` binds no value: it can only suppress.
    assert intros["z"][0][3] is None


def test_a_block_scoped_declaration_governs_only_its_block() -> None:
    source = "function f() {\n  if (ok) { let v = 1; }\n  v;\n}\n"
    root = _root(source)
    (intro,) = scope_introductions(first(root, "function_declaration"))["v"]
    assert innermost_enclosing((intro,), source.index("let v"))[0]
    assert innermost_enclosing((intro,), source.index("  v;")) == (False, None)


def test_the_innermost_of_several_introductions_decides() -> None:
    outer = (0, 100, DECL_PLAIN, None)
    inner = (10, 20, DECL_PARAM, None)
    assert innermost_enclosing((outer, inner), 15) == (True, (DECL_PARAM, None))
    assert innermost_enclosing((outer, inner), 50) == (True, (DECL_PLAIN, None))
    # Two at the same depth tie: decided, with no answer.
    tie = (10, 20, DECL_PLAIN, None)
    assert innermost_enclosing((inner, tie), 15) == (True, None)


def test_named_functions_maps_each_name_to_its_callable() -> None:
    root = _root(
        "function make() {}\nconst build = () => 1;\nconst value = 3;\n"
        "function outer() { const inner = function () {}; }\n"
    )
    functions = named_functions(root)
    # A declarator binding no callable names no function.
    assert set(functions) == {"make", "build", "outer", "inner"}
    assert functions["make"].type == "function_declaration"
    assert functions["build"].type == "arrow_function"
    assert functions["inner"].type == "function_expression"


def test_the_file_index_lists_every_introduction_of_a_name() -> None:
    root = _root(
        "import { api } from './api';\n"
        "const api2 = api;\n"
        "function f(api) { api.call(); }\n"
    )
    bindings = JsFileBindingCollector(unwrap_ts_value).collect(root)
    # The import is file-wide; the parameter is contained by its function.
    containers = sorted(
        container.type if container is not None else ""
        for _, container in bindings["api"]
    )
    assert containers == ["", "function_declaration"]
    # A declared function is introduced with its own node, in the module.
    ((function_node, container),) = bindings["f"]
    assert function_node is not None
    assert function_node.type == "function_declaration"
    assert container is not None
    assert container.type == "program"


def test_a_with_block_makes_every_name_dynamic() -> None:
    root = _root("const a = 1;\nwith (obj) { a; }\n")
    assert JsFileBindingCollector(unwrap_ts_value).collect(root) == {}


def test_a_namespace_merges_its_exported_members_across_blocks() -> None:
    root = _root(
        "namespace N { export const shared = 1; const own = 2; }\n"
        "namespace N { shared; }\n",
        cs.SupportedLanguage.TS,
    )
    bindings = JsFileBindingCollector(unwrap_ts_value).collect(root)
    # The exported member is visible in the sibling block too; the other one
    # stays its own block's.
    assert len(bindings["shared"]) == 2
    assert len(bindings["own"]) == 1
