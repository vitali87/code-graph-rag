"""Julia's share of the call pass: the name a Julia definition's body calls are
attributed under (a definition node carries no `name` field), read the way
the definition pass registers it, the calls an anonymous arrow leaves to its
enclosing scope, and the callee a call names.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver)
and hands Julia's part to the `JuliaCallResolution` it builds."""

from __future__ import annotations

from typing import Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionLocation
from ..language_calls import CallName, LanguageCalls
from . import utils as julia_utils


class JuliaCallHost(Protocol):
    """What Julia's caller naming reads on the call pass that builds it: the
    location the definition pass recorded for a function node."""

    def _recorded_caller(
        self, func_node: Node, module_qn: str
    ) -> FunctionLocation | None: ...


class JuliaCallResolution(LanguageCalls):
    """Names Julia callers as the definition pass registers them.

    Built and owned by `CallProcessor`, whose recorded definition locations
    it reads."""

    __slots__ = ("_host",)

    # A named nested definition gets its own caller node and keeps its own
    # calls.
    owns_nested_calls = True
    separate_macro_namespace = True
    functions_shadow_types = True

    def __init__(self, host: JuliaCallHost) -> None:
        self._host = host

    def caller_name(self, func_node: Node, module_qn: str) -> str | None:
        # Julia definition nodes carry no `name` field; recover it
        # here or the body is skipped.
        return (
            super().caller_name(func_node, module_qn)
            or julia_utils.function_head_name(func_node)
            or julia_utils.arrow_assigned_name(func_node)
            or None
        )

    def method_name(self, method_node: Node, module_qn: str) -> str | None:
        # Same name recovery as the module pass, else the inner
        # constructor's body is skipped (issue #1882).
        return super().method_name(method_node, module_qn) or self.registered_name(
            method_node, module_qn
        )

    def call_name(self, call_node: Node) -> CallName:
        # A definition's own head IS a call_expression; drop heads
        # here (no query negation to exclude them at capture time).
        if julia_utils.is_signature_head(call_node):
            return CallName(True, None)
        return CallName(True, julia_utils.call_name(call_node))

    def attributable(self, func_nodes: list[Node], module_qn: str) -> list[Node]:
        # An anonymous Julia arrow gets no caller node: its calls
        # bubble to the enclosing scope like JS/TS arrows.
        return [n for n in func_nodes if self.registered_name(n, module_qn)]

    def registered_name(self, func_node: Node, module_qn: str) -> str | None:
        # The name the definition pass registers for this node, or None
        # while it stays anonymous; every caller pass derives attribution
        # through this helper so both passes name a node identically.
        name = julia_utils.function_head_name(
            func_node
        ) or julia_utils.arrow_assigned_name(func_node)
        if name:
            return name
        if (
            recorded := self._host._recorded_caller(func_node, module_qn)
        ) is not None and recorded.is_named:
            return recorded.qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        return None
