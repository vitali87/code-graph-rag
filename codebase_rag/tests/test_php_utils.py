"""PHP naming helpers: an anonymous class's positional name, its qn segment
under the callable it is written in, where the scope walk above it
resumes, and a PHP function's positional parameter slots."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.php.utils import (
    anonymous_class_anchor_stop,
    anonymous_class_name,
    anonymous_class_scope_name,
    php_positional_parameter_slots,
)
from codebase_rag.tests.collaborator_support import first, nodes_of, parse

_DISPATCHER = """<?php
class Dispatcher {
    public function add() {
        $o = new class {};
        $f = function () { return new class {}; };
    }
    public function run() {
        function inner() { return new class {}; }
        $g = function () { $h = fn() => new class {}; };
    }
}
$top = new class {};
"""


def _anonymous_classes() -> list[Node]:
    return nodes_of(parse(cs.SupportedLanguage.PHP, _DISPATCHER), "anonymous_class")


def test_an_anonymous_class_is_named_by_its_position() -> None:
    names = [anonymous_class_name(node) for node in _anonymous_classes()]
    assert names == [
        "anonymous_3_17",
        "anonymous_4_38",
        "anonymous_7_38",
        "anonymous_8_44",
        "anonymous_11_11",
    ]


def test_a_named_class_has_no_anonymous_name_scope_or_stop() -> None:
    named = first(parse(cs.SupportedLanguage.PHP, _DISPATCHER), "class_declaration")
    assert anonymous_class_name(named) is None
    assert anonymous_class_scope_name(named) is None
    assert anonymous_class_anchor_stop(named) is None


def test_the_scope_name_spells_the_callable_as_it_is_registered() -> None:
    scopes = [anonymous_class_scope_name(node) for node in _anonymous_classes()]
    assert scopes == [
        # Directly in a method: under the method's name.
        "add.anonymous_3_17",
        # In a closure in `add`: under the closure's own positional name.
        "add.anonymous_4_13.anonymous_4_38",
        # A named function declared inside `run` registers under its type
        # scope alone, so `run` is not part of the class's segment.
        "inner.anonymous_7_38",
        # An arrow fn inside a closure in `run`: the enclosing closure adds
        # nothing, only the named callables around the innermost do.
        "run.anonymous_8_32.anonymous_8_44",
        # At file level there is no callable to name.
        "anonymous_11_11",
    ]


def test_the_walk_resumes_at_the_nearest_type_or_the_program() -> None:
    stops = [anonymous_class_anchor_stop(node) for node in _anonymous_classes()]
    assert [stop.type if stop is not None else None for stop in stops] == [
        "class_declaration",
        "class_declaration",
        "class_declaration",
        "class_declaration",
        "program",
    ]


def test_simple_parameters_fill_their_slot_with_the_dollar_name() -> None:
    func = first(
        parse(cs.SupportedLanguage.PHP, "<?php\nfunction g($a, int &$b = 1) {}\n"),
        cs.TS_PHP_FUNCTION_DEFINITION,
    )
    assert php_positional_parameter_slots(func) == (["$a", "$b"], None)


def test_promoted_and_variadic_parameters_hold_an_empty_slot() -> None:
    method = first(
        parse(
            cs.SupportedLanguage.PHP,
            "<?php\nclass C {\n"
            "    public function __construct(private int $p, $q, int ...$rest) {}\n"
            "}\n",
        ),
        cs.TS_PHP_METHOD_DECLARATION,
    )
    # The slots stay positional, so `$q` is still the second argument.
    assert php_positional_parameter_slots(method) == ([None, "$q", None], None)


def test_a_node_without_parameters_has_no_slots() -> None:
    class_node = first(
        parse(cs.SupportedLanguage.PHP, "<?php\nclass C {}\n"), "class_declaration"
    )
    assert php_positional_parameter_slots(class_node) == ([], None)
