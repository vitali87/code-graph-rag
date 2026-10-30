"""Java's share of the call pass: the declaration a Java call binds through
the javac frontend or the Java engine, the same-arity overloads it cannot
tell apart, the methods a method reference hands over, and the parameter
signature a Java method's qualified name carries.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver,
the class pass's qualified names and the construction a `new` or a
`Type::new` runs) and hands Java's part to the `JavaCallResolution` it
builds."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ..edge_site import EdgeSiteHost
from ..language_calls import LanguageCalls
from . import utils as java_utils
from .type_inference import JAVA_EXTERNAL_TARGET

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class JavaCallHost(EdgeSiteHost, Protocol):
    """What Java call resolution sets and calls on the call pass that builds
    it. Beside the edge site, `_verdict`, how surely the call node being
    scanned binds its callee, which a tie between overloads lowers to
    `overload`; and the record of a name no definition answers yet, which a
    file added later may define."""

    _verdict: str

    def _note_unresolved(self, module_qn: str, name: str) -> None: ...


class JavaCallScan(Protocol):
    """The caller a Java call node is scanned in."""

    caller_spec: tuple[str, str, str]
    caller_qn: str
    module_qn: str
    local_var_types: dict[str, str] | None


def method_member(func_node: Node, func_name: str) -> str:
    # A Java Method node is registered with its parameter signature
    # (definition pass: class_qn.name(params)), so the caller endpoint of a
    # CALLS edge must carry the same signature to join that node. Mirrors
    # class_ingest.ingest's method-qn build exactly.
    info = java_utils.extract_method_info(func_node)
    name = info.get(cs.KEY_NAME) or func_name
    parameters = info.get(cs.KEY_PARAMETERS, [])
    param_sig = f"({','.join(parameters)})" if parameters else cs.EMPTY_PARENS
    return f"{name}{param_sig}"


class JavaCallResolution(LanguageCalls):
    """Binds Java calls through the javac frontend or the Java engine, links
    the overloads a binding cannot tell apart, and writes the references a
    method reference makes.

    Built and owned by `CallProcessor`. The resolver is the pass's own, held
    by reference: the definition pass fills its registry as it registers
    each file."""

    __slots__ = ("_host", "_resolver")

    def __init__(self, host: JavaCallHost, resolver: CallResolver) -> None:
        self._host = host
        self._resolver = resolver

    def resolve_callee(
        self,
        ctx: JavaCallScan,
        call_node: Node,
        ensure_rel: Callable[..., None],
    ) -> tuple[str, str] | None:
        # The javac frontend (issue #1181): a HIT is the declaration the
        # compiler bound this call to, which the ARGUMENT TYPES select
        # and name-and-arity matching cannot; the external sentinel is
        # its proof that the call leaves the repo, so no first-party
        # edge is fabricated. Any miss (incl. the frontend being off, so
        # both fact maps are empty) falls back to today's Java resolver.
        callee_info = self._resolver.java.resolve_java_call_site(
            call_node, ctx.module_qn
        )
        if callee_info == JAVA_EXTERNAL_TARGET:
            callee_info = None
        elif callee_info is None:
            callee_info = self._resolver.java.resolve_java_method_call(
                call_node, ctx.module_qn, ctx.local_var_types, ctx.caller_qn
            )
            if callee_info is not None:
                self._link_overload_ties(ctx, call_node, callee_info[1], ensure_rel)
        return callee_info

    def _link_overload_ties(
        self,
        ctx: JavaCallScan,
        call_node: Node,
        callee_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        # Same-arity overloads the argument types cannot tell apart were
        # bound by declaration order, which is no evidence for any one of
        # them: link each, labelled `overload`, as a constructor family is
        # (issue #2548). The resolver's verdict carries the label onto the
        # primary edge, which the caller emits after the argument passes;
        # the edge label proper is scoped to the siblings, so the argument
        # references emitted in between keep their own.
        java = self._resolver.type_inference.java_type_inference
        ties = java.method_resolver.java_overload_ties(
            call_node, callee_qn, ctx.local_var_types, ctx.module_qn
        )
        if not ties:
            return
        self._host._verdict = cs.EdgeResolution.OVERLOAD
        prev_resolution = self._host._resolution
        self._host._resolution = cs.EdgeResolution.OVERLOAD
        registry = self._resolver.function_registry
        try:
            for target_type, target_qn in ties:
                if target_qn == callee_qn:
                    continue
                for variant in registry.variants(target_qn):
                    ensure_rel(
                        ctx.caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                    )
        finally:
            self._host._resolution = prev_resolution

    def ingest_method_reference(
        self,
        ctx: JavaCallScan,
        ref_node: Node,
        ensure_rel: Callable[..., None],
    ) -> Node | None:
        # `Type::m`, `expr::m`, `this::m`, `super::m` hand a method over as a
        # value and nothing runs at this site, so the edge is REFERENCES: what
        # a C# method group and a callback argument get in every other
        # language, which keeps the method reachable without asserting a
        # call the graph cannot see (issue #2546). A `Type::new` writes none
        # here: its receiver is returned for the call pass to construct.
        if (parts := java_utils.method_reference_parts(ref_node)) is None:
            return None
        if parts.method_name is None:
            return parts.receiver
        java = self._resolver.type_inference.java_type_inference
        targets = java.method_resolver.java_method_reference_targets(
            parts.receiver, parts.method_name, ctx.local_var_types, ctx.module_qn
        )
        registry = self._resolver.function_registry
        edges = [
            (label, variant)
            for target_type, target_qn in targets
            for variant in registry.variants(target_qn)
            if (label := registry.get(variant) or target_type)
            in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        ]
        if not edges:
            # Java sees same-package definitions without an import, so the
            # name is the only link to a file that may define it later.
            self._host._note_unresolved(ctx.module_qn, parts.method_name)
            return None
        self._host._resolution = (
            cs.EdgeResolution.OVERLOAD if len(edges) > 1 else self._host._verdict
        )
        for label, target_qn in edges:
            ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.REFERENCES,
                (label, cs.KEY_QUALIFIED_NAME, target_qn),
            )
        return None
