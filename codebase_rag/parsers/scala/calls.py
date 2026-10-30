"""Scala's share of the call pass: the callee a Scala call binds the way the
compiler looks it up, the parameterless method a bare selection (`c.size`)
calls, and the auxiliary constructors a `new C(...)` may run.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver
and the edges a resolved call writes) and hands Scala's part to the
`ScalaCallResolution` it builds."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...node_text import decode_node_text
from ...types_defs import ScalaBinding
from ..language_calls import LanguageCalls
from . import utils as scala_utils

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class ScalaCallScan(Protocol):
    """The caller a Scala call node is scanned in, and the bindings built on
    it once, on first use, for all of that caller's selections."""

    caller_node: Node
    module_qn: str
    class_context: str | None
    scala_bindings: dict[str, list[ScalaBinding]] | None


class ScalaCallee(NamedTuple):
    # What a Scala call binds to through the compiler's lookup. `decided`
    # False leaves the call to the name-based resolver; True means `callee`
    # is the answer, and None there is a known non-edge.
    decided: bool
    callee: tuple[str, str] | None


class ScalaCallResolution(LanguageCalls):
    """Binds Scala calls and selections through the compiler's lookup and
    writes the auxiliary-constructor calls a construction makes.

    Built and owned by `CallProcessor`. The resolver is the pass's own, held
    by reference: the definition pass fills its registry as it registers
    each file."""

    __slots__ = ("_resolver",)

    new_constructs_type = True

    def __init__(self, resolver: CallResolver) -> None:
        self._resolver = resolver

    def node_call_name(self, call_node: Node) -> str | None:
        match call_node.type:
            case cs.TS_SCALA_INSTANCE_EXPRESSION:
                # Scala `new C(...)` holds its type as an unnamed child, so
                # it fell through nameless and emitted nothing (issue #2450).
                return scala_utils.instance_type_name(call_node)
            # Infix operator call (`a ~> b`, `xs map f`): the callee is the
            # `operator` field's method name; tree-sitter has no `function`
            # field here. Infix is unambiguously a method call; a bare
            # `field_expression` (`obj.done` with no parens) is deliberately
            # NOT named here because Scala's uniform access makes a nullary
            # call and a `val` read syntactically identical, so resolving it
            # by simple name would turn a same-named field read into a
            # spurious CALLS edge.
            case cs.TS_SCALA_INFIX_EXPRESSION:
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    return decode_node_text(operator_node.text)
        return None

    def resolve_callee(
        self, ctx: ScalaCallScan, call_node: Node, call_name: str
    ) -> ScalaCallee:
        # Named the way the compiler looks it up: the imports in scope at
        # the call, the file, its package's other files. A path-qualified
        # `new shop.Cart()` names no import, so only the package index
        # reaches it.
        return ScalaCallee(
            *self._resolver.scala.resolve_scala_callee(
                call_name,
                ctx.module_qn,
                call_node.start_byte,
                constructing=call_node.type == cs.TS_SCALA_INSTANCE_EXPRESSION,
            )
        )

    def selection_callee(
        self, ctx: ScalaCallScan, node: Node
    ) -> tuple[str, tuple[str, str]] | None:
        # `c.size` with no parentheses: a parameterless method call, or a
        # field read spelled the same way. It binds only through a receiver
        # whose class is known (issue #2450); the selection that is a call's
        # callee is left to the call node, which names it with its args.
        # Returns the call name and the callee the call pass ingests.
        selection = scala_utils.selection(node)
        if selection is None:
            return None
        if ctx.scala_bindings is None:
            ctx.scala_bindings = scala_utils.bindings(ctx.caller_node)
        receiver, member = selection
        binding = scala_utils.binding_at(ctx.scala_bindings, receiver, node.start_byte)
        # A local's binding types it; an unbound name is an object named
        # outright. A local of unknown type is neither, so it binds nothing.
        receiver_type = binding.type_name if binding is not None else receiver
        if receiver_type is None:
            return None
        callee_info = self._resolver.scala.resolve_scala_selection(
            receiver_type, member, ctx.module_qn, ctx.class_context, node.start_byte
        )
        if callee_info is None:
            return None
        return f"{receiver}{cs.SEPARATOR_DOT}{member}", callee_info

    def emit_auxiliary_ctor_calls(
        self,
        caller_spec: tuple[str, str, str],
        class_variants: list[str],
        ensure_rel: Callable[..., None],
    ) -> None:
        # A Scala class's primary constructor is its body, which has no node
        # of its own; an auxiliary `def this(...)` is a method, and any of
        # them may be the one `new C(...)` runs.
        registry = self._resolver.function_registry
        for class_variant in class_variants:
            ctor_qn = (
                f"{class_variant}{cs.SEPARATOR_DOT}{cs.SCALA_AUXILIARY_CONSTRUCTOR}"
            )
            if registry.get(ctor_qn) != cs.NodeLabel.METHOD:
                continue
            for variant in registry.variants(ctor_qn):
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, variant),
                )
