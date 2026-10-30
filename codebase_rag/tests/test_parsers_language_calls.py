"""The base `LanguageCalls`: the answers the call pass gets from a language
that adds nothing to it, which every language's call collaborator inherits
for the hooks it leaves alone."""

from __future__ import annotations

from typing import ClassVar

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.language_calls import (
    UNDECIDED_CALLEE,
    UNDECIDED_NAME,
    Callee,
    CallName,
    LanguageCalls,
)

_CAPABILITIES = (
    "flows_callables",
    "references_arguments",
    "scans_callers_without_calls",
    "anonymous_calls_reach_module",
    "binds_callable_fields",
    "keys_members_by_symbol",
    "owns_nested_calls",
    "types_locals",
    "new_constructs_type",
    "binds_builtin_calls",
    "binds_operator_calls",
    "separate_macro_namespace",
    "runs_declared_constructors",
    "inherits_constructors",
    "functions_shadow_types",
    "dispatches_protocol_stubs",
    "fans_out_member_aliases",
    "short_circuit_values",
    "property_args_are_reads",
    "imports_bind_calls",
)

_SOURCE = b"class Shape:\n    def area(self):\n        return measure(self)\n\nf = lambda: 1\n"


def _first(root: Node, node_type: str) -> Node:
    stack = [root]
    while stack:
        node = stack.pop(0)
        if node.type == node_type:
            return node
        stack.extend(node.children)
    raise AssertionError(node_type)


def _python_nodes() -> tuple[Node, Node, Node, Node]:
    parsers, _ = load_parsers()
    root = parsers[cs.SupportedLanguage.PYTHON].parse(_SOURCE).root_node
    return (
        _first(root, cs.TS_PY_CLASS_DEFINITION),
        _first(root, cs.TS_PY_FUNCTION_DEFINITION),
        _first(root, cs.TS_PY_CALL),
        _first(root, cs.TS_PY_LAMBDA),
    )


def test_the_base_turns_on_no_capability() -> None:
    calls = LanguageCalls()
    assert {name: getattr(calls, name) for name in _CAPABILITIES} == dict.fromkeys(
        _CAPABILITIES, False
    )
    assert calls.constructor_member == cs.PY_METHOD_INIT


def test_a_capability_is_opted_into_on_the_subclass_alone() -> None:
    class _Flows(LanguageCalls):
        __slots__ = ()
        flows_callables: ClassVar[bool] = True

    assert _Flows().flows_callables is True
    assert LanguageCalls().flows_callables is False


def test_names_are_read_off_the_name_field() -> None:
    class_node, method, _call, lam = _python_nodes()
    calls = LanguageCalls()
    assert calls.class_name(class_node) == "Shape"
    assert calls.method_name(method, "proj.mod") == "area"
    assert calls.caller_name(method, "proj.mod") == "area"
    assert calls.member_name(method, "area") == "area"
    # A function with no name of its own gets no caller pass.
    assert calls.caller_name(lam, "proj.mod") is None


def test_the_base_leaves_every_call_to_the_shared_reading() -> None:
    _class_node, method, call, _lam = _python_nodes()
    calls = LanguageCalls()
    assert calls.call_name(call) == UNDECIDED_NAME == CallName(False, None)
    assert calls.function_field_name(call) is None
    assert calls.node_call_name(call) is None
    assert calls.class_qn(method, "proj.mod", "Shape") is None
    assert calls.function_qn(method, "proj.mod", "area") is None
    assert calls.bound_method_caller(method, "area", "proj.mod") is None
    assert calls.scope_name(method) is None
    assert calls.parameter_names(method) is None
    assert calls.template_params(method) == frozenset()


def test_the_base_passes_what_it_is_given_through() -> None:
    _class_node, method, call, _lam = _python_nodes()
    calls = LanguageCalls()
    assert calls.caller_calls(method, [call]) == [call]
    assert calls.attributable([method], "proj.mod") == [method]
    assert calls.local_type_spans(method, "proj.mod", {"s": "Shape"}) == []
    assert calls.span_bindings(method, "proj.mod", None, {"s": "Shape"}) == []
    assert calls.build_targets("proj.mod", "Function", "proj.f", ["proj.f"]) == (
        ["proj.f"],
        {},
    )


def test_resolution_hooks_stay_undecided() -> None:
    assert UNDECIDED_CALLEE == Callee(False, None)
    # Undecided and a known non-edge are different answers.
    assert Callee(True, None) != UNDECIDED_CALLEE
