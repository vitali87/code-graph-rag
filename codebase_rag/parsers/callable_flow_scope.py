"""Which functions of a file take part in callable-argument flow.

A function that invokes a parameter (`def run_callback(fn): return fn()`)
gets an edge to every callable its callers pass in, but the edge is derived
from bindings recorded while walking the CALLERS' files. An incremental run
that re-parses such a function's file must walk those callers too, and,
through a parameter a caller only forwards (`def wrap(cb): return
run_callback(cb)`), their callers in turn (issue #2911).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from tree_sitter import Node

from .. import constants as cs
from .handlers import get_handler
from .utils import callable_parameter_indices, python_parameter_names, safe_decode_text


class FileFlowFunctions:
    """The functions of one file that invoke a parameter, and those that may
    forward one into such a function."""

    __slots__ = ("invokers", "_forwarding", "_aliases")

    def __init__(
        self,
        function_nodes: Iterable[Node],
        language: cs.SupportedLanguage,
        root: Node,
    ) -> None:
        invokers: set[str] = set()
        forwarding: list[tuple[str, Node]] = []
        # A C++ definition names itself in its declarator and a JS/TS arrow
        # function through the variable it is bound to.
        handler = get_handler(language)
        for func_node in function_nodes:
            name = handler.extract_function_name(func_node)
            if not name:
                continue
            if callable_parameter_indices(func_node, language):
                invokers.add(name)
            # Pass-through flow is traced for Python arguments only.
            if language == cs.SupportedLanguage.PYTHON:
                forwarding.append((name, func_node))
        self.invokers = frozenset(invokers)
        self._forwarding = tuple(forwarding)
        self._aliases = _python_import_aliases(root) if forwarding else {}

    def forwarders(self, flow_names: frozenset[str]) -> set[str]:
        """Simple names of the functions that forward a parameter into a call
        to a function named in `flow_names`."""
        if not flow_names:
            return set()
        return {
            name
            for name, func_node in self._forwarding
            if _forwards_parameter_into(func_node, flow_names, self._aliases)
        }

    def names(self, flow_names: frozenset[str]) -> frozenset[str]:
        return self.invokers | self.forwarders(flow_names)


def settled_flow_names(
    files: Collection[FileFlowFunctions], flow_names: frozenset[str]
) -> frozenset[str]:
    """`flow_names` grown until no file adds one: a wrapper read before the
    function it forwards into is read again once that name is known."""
    while True:
        grown = flow_names.union(*(file.names(flow_names) for file in files))
        if grown == flow_names:
            return flow_names
        flow_names = grown


def _python_import_aliases(root: Node) -> dict[str, str]:
    # `from registry import run_callback as invoke`: a call of `invoke`
    # resolves to `run_callback`, the name the flow knows it by.
    aliases: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_ALIASED_IMPORT:
            name = node.child_by_field_name(cs.FIELD_NAME)
            alias = node.child_by_field_name(cs.FIELD_ALIAS)
            if name is not None and alias is not None:
                original = safe_decode_text(name)
                if (local := safe_decode_text(alias)) and original:
                    aliases[local] = original.rpartition(cs.SEPARATOR_DOT)[2]
            continue
        stack.extend(node.children)
    return aliases


def _forwards_parameter_into(
    func_node: Node, flow_names: frozenset[str], aliases: Mapping[str, str]
) -> bool:
    params = set(python_parameter_names(func_node))
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if not params or body is None:
        return False
    stack = [body]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_CALL and _call_forwards(
            node, params, flow_names, aliases
        ):
            return True
        stack.extend(node.children)
    return False


def _call_forwards(
    call: Node,
    params: set[str],
    flow_names: frozenset[str],
    aliases: Mapping[str, str],
) -> bool:
    callee = call.child_by_field_name(cs.FIELD_FUNCTION)
    if callee is None:
        return False
    if callee.type == cs.TS_PY_ATTRIBUTE:
        member = callee.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE)
        name = safe_decode_text(member) if member is not None else None
    elif text := safe_decode_text(callee):
        name = aliases.get(text, text)
    else:
        name = None
    if name not in flow_names:
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
