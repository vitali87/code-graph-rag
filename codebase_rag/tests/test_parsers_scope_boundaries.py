"""`flow_scope_boundaries`: the nested function and class scopes a walk over
one scope's own syntax stops at. Python's three are in every language's set;
each language adds its own function and class node types."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parsers.scope_boundaries import (
    PY_SCOPE_BOUNDARY_TYPES,
    flow_scope_boundaries,
)
from codebase_rag.tests.collaborator_support import first, parse

_PY_OUTER = """\
def outer():
    def inner():
        return "inner"
    class Local:
        def method(self):
            return "method"
    @wrap
    def decorated():
        return "decorated"
    pick = lambda: "lambda"
    if ready:
        return "branch"
    return "outer"
"""

_JS_OUTER = """\
function outer() {
  const f = () => { return "arrow"; };
  const g = function () { return "expression"; };
  class K { m() { return "method"; } }
  if (ready) { return "branch"; }
  return "outer";
}
"""


def _own_strings(scope: Node, boundaries: frozenset[str]) -> list[str]:
    # The string literals a walk over `scope` reaches when it stops where the
    # call pass's walkers stop: which scope's syntax each belongs to.
    found: list[str] = []
    stack = list(scope.children)
    while stack:
        node = stack.pop()
        if node.type in boundaries:
            continue
        if node.type in ("string", "string_fragment") and node.text is not None:
            text = node.text.decode().strip("\"'")
            if text:
                found.append(text)
        stack.extend(node.children)
    return sorted(set(found))


def test_a_python_walk_stops_at_nested_defs_classes_and_decorated_definitions() -> None:
    outer = first(
        parse(cs.SupportedLanguage.PYTHON, _PY_OUTER), cs.TS_PY_FUNCTION_DEFINITION
    )
    boundaries = flow_scope_boundaries(LANGUAGE_SPECS[cs.SupportedLanguage.PYTHON])

    # A lambda is no scope of its own here and an `if` block is no scope at
    # all, so both stay the enclosing function's.
    assert _own_strings(outer, boundaries) == ["branch", "lambda", "outer"]
    # Without the boundaries the same walk reaches every nested scope.
    assert _own_strings(outer, frozenset()) == [
        "branch",
        "decorated",
        "inner",
        "lambda",
        "method",
        "outer",
    ]


def test_a_js_walk_stops_at_arrows_function_expressions_and_classes() -> None:
    outer = first(parse(cs.SupportedLanguage.JS, _JS_OUTER), "function_declaration")
    boundaries = flow_scope_boundaries(LANGUAGE_SPECS[cs.SupportedLanguage.JS])

    assert _own_strings(outer, boundaries) == ["branch", "outer"]


def test_every_language_keeps_the_python_set_and_adds_its_own_types() -> None:
    for spec in LANGUAGE_SPECS.values():
        boundaries = flow_scope_boundaries(spec)
        assert PY_SCOPE_BOUNDARY_TYPES <= boundaries, spec.language
        assert frozenset(spec.function_node_types) <= boundaries, spec.language
        assert frozenset(spec.class_node_types) <= boundaries, spec.language


def test_the_python_set_names_the_decorated_wrapper_the_spec_lacks() -> None:
    spec = LANGUAGE_SPECS[cs.SupportedLanguage.PYTHON]
    assert cs.TS_PY_DECORATED_DEFINITION not in spec.function_node_types
    assert cs.TS_PY_DECORATED_DEFINITION in flow_scope_boundaries(spec)
    assert PY_SCOPE_BOUNDARY_TYPES == {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
