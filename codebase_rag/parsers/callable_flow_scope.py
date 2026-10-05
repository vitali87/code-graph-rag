"""Which functions of a file take part in callable-argument flow.

A function that invokes a parameter (`def run_callback(fn): return fn()`)
gets an edge to every callable its callers pass in, but the edge is derived
from bindings recorded while walking the CALLERS' files. An incremental run
that re-parses such a function's file must walk those callers too, and,
through a parameter a caller only forwards (`def wrap(cb): return
run_callback(cb)`), their callers in turn (issue #2911).
"""

from __future__ import annotations

from collections.abc import Iterable

from tree_sitter import Node

from .. import constants as cs
from .utils import callable_parameter_indices, python_parameter_names, safe_decode_text


def flow_function_names(
    function_nodes: Iterable[Node],
    language: cs.SupportedLanguage,
    flow_names: frozenset[str],
) -> set[str]:
    """Simple names of the functions that invoke a parameter or, in Python,
    forward one into a call to a function named in `flow_names`."""
    names: set[str] = set()
    for func_node in function_nodes:
        name_node = func_node.child_by_field_name(cs.FIELD_NAME)
        name = safe_decode_text(name_node) if name_node is not None else None
        if not name:
            continue
        if callable_parameter_indices(func_node, language) or (
            # Pass-through flow is recorded for Python arguments only.
            language == cs.SupportedLanguage.PYTHON
            and flow_names
            and _forwards_parameter_into(func_node, flow_names)
        ):
            names.add(name)
    return names


def _forwards_parameter_into(func_node: Node, flow_names: frozenset[str]) -> bool:
    params = set(python_parameter_names(func_node))
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if not params or body is None:
        return False
    stack = [body]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_CALL and _call_forwards(node, params, flow_names):
            return True
        stack.extend(node.children)
    return False


def _call_forwards(call: Node, params: set[str], flow_names: frozenset[str]) -> bool:
    callee = call.child_by_field_name(cs.FIELD_FUNCTION)
    if callee is not None and callee.type == cs.TS_PY_ATTRIBUTE:
        callee = callee.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE)
    if callee is None or safe_decode_text(callee) not in flow_names:
        return False
    arguments = call.child_by_field_name(cs.FIELD_ARGUMENTS)
    for argument in arguments.named_children if arguments is not None else ():
        if argument.type == cs.TS_PY_KEYWORD_ARGUMENT:
            argument = argument.child_by_field_name(cs.FIELD_VALUE)
        if (
            argument is not None
            and argument.type == cs.TS_PY_IDENTIFIER
            and safe_decode_text(argument) in params
        ):
            return True
    return False
