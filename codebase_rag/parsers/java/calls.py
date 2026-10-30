"""Java's share of the call pass: the declaration a Java call binds through
the javac frontend or the Java engine, the same-arity overloads it cannot
tell apart, the methods and constructors a method reference hands over, the
constructors an enum constant runs, and the parameter signature a Java
method's qualified name carries.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver,
the class pass's qualified names and the construction a `new` runs) and
hands Java's part to the `JavaCallResolution` it builds."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import LanguageQueries
from ..call_host import RecordedCallerHost, VerdictHost
from ..class_ingest.identity import build_nested_qualified_name_for_class
from ..edge_site import EdgeWriterHost, host_site_scoped
from ..language_calls import UNDECIDED_CALLEE, Callee, CallScan, LanguageCalls
from ..utils import written_simple_name
from . import utils as java_utils
from .type_inference import JAVA_EXTERNAL_TARGET

if TYPE_CHECKING:
    from ..call_callers import CallerScan
    from ..call_construction import CallConstruction
    from ..call_resolver import CallResolver


class JavaCallHost(EdgeWriterHost, VerdictHost, RecordedCallerHost, Protocol):
    """What Java call resolution sets and calls on the call pass that builds
    it. Beside the edge site, the verdict, which a tie between overloads
    lowers to `overload`; the record of a name no definition answers yet,
    which a file added later may define; the construction a `Type::new`
    references, as the pass constructs a `new`; and, for the constructors an
    enum constant runs, the edge emitter, a definition's recorded location
    and own name, and the caller scan, which gives a class's member the
    qualified name the class pass does."""

    def _note_unresolved(self, module_qn: str, name: str) -> None: ...

    def _get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None: ...

    @property
    def construction(self) -> CallConstruction: ...

    @property
    def callers(self) -> CallerScan: ...


class JavaCallScan(Protocol):
    """The caller a Java call node is scanned in."""

    caller_spec: tuple[str, str, str]
    caller_qn: str
    module_qn: str
    language: cs.SupportedLanguage
    class_context: str | None
    local_var_types: dict[str, str] | None
    resolve_func: Callable[..., tuple[str, str] | None]
    ensure_rel: Callable[..., None]


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
    method reference makes and the constructor calls an enum constant makes.

    Built and owned by `CallProcessor`. The resolver is the pass's own, held
    by reference: the definition pass fills its registry as it registers
    each file."""

    __slots__ = ("_host", "_resolver")

    types_locals = True
    new_constructs_type = True
    runs_declared_constructors = True

    def __init__(self, host: JavaCallHost, resolver: CallResolver) -> None:
        self._host = host
        self._resolver = resolver

    def member_name(self, func_node: Node, func_name: str) -> str:
        return method_member(func_node, func_name)

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        # A constant runs its enum's constructor without any call
        # expression, so a file holding only an enum has to be covered
        # before the no-calls early return (issue #2547).
        self._ingest_enum_constant_ctor_calls(
            module_qn, combined_captures.get(cs.CAPTURE_CLASS) or [], queries
        )

    @host_site_scoped
    def _ingest_enum_constant_ctor_calls(
        self,
        module_qn: str,
        class_nodes: list[Node],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        # Outside its own constructors, `CLASS("c")` is the only thing that runs
        # a Java enum constructor (`new` on an enum is a compile error), and no
        # call node names it, so every parameterised enum constructor looked
        # dead. Each constant CALLS the constructors its argument count admits;
        # one candidate is the compiler's choice, several (same arity) fan out
        # as `overload`, the way `new X(...)` does. No INSTANTIATES: that edge
        # only targets Class.
        host = self._host
        for enum_node in class_nodes:
            if enum_node.type != cs.TS_ENUM_DECLARATION:
                continue
            constants = java_utils.enum_constants(enum_node)
            if not constants:
                continue
            ctors = [
                (ctor, target)
                for ctor in java_utils.enum_constructors(enum_node)
                if (
                    target := self._enum_ctor_target(
                        ctor, enum_node, module_qn, queries
                    )
                )
                is not None
            ]
            if not ctors:
                continue
            caller_spec = self._enum_initializer(enum_node, module_qn)
            for constant in constants:
                arg_count = java_utils.argument_count(constant)
                targets = [
                    target
                    for ctor, target in ctors
                    if java_utils.accepts_argument_count(ctor, arg_count)
                ]
                host._site_node = constant
                host._resolution = (
                    cs.EdgeResolution.EXACT
                    if len(targets) == 1
                    else cs.EdgeResolution.OVERLOAD
                )
                for target_label, target_qn in targets:
                    # A bare `NONE` has no argument list for the site to
                    # count, yet it is a zero-argument invocation all the same.
                    host.emit_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_label, cs.KEY_QUALIFIED_NAME, target_qn),
                        {cs.KEY_ARG_COUNT: arg_count, cs.KEY_KWARG_NAMES: []},
                    )

    def _enum_ctor_target(
        self,
        ctor_node: Node,
        enum_node: Node,
        module_qn: str,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> tuple[str, str] | None:
        host = self._host
        if loc := host.recorded_caller(ctor_node, module_qn):
            return loc.label, loc.qualified_name
        # The class pass's own derivation, for a run that did not re-record
        # this file's definitions.
        enum_name = host._get_node_name(enum_node)
        ctor_name = host._get_node_name(ctor_node)
        if not enum_name or not ctor_name:
            return None
        language = cs.SupportedLanguage.JAVA
        java_config = queries[language][cs.QUERY_CONFIG]
        enum_qn = (
            build_nested_qualified_name_for_class(
                enum_node, module_qn, enum_name, java_config
            )
            or f"{module_qn}{cs.SEPARATOR_DOT}{enum_name}"
        )
        target_qn, target_label = host.callers.class_member_qn_and_label(
            ctor_node, enum_qn, ctor_name, java_config, language
        )
        if target_qn not in self._resolver.function_registry:
            return None
        return target_label, target_qn

    def _enum_initializer(
        self, enum_node: Node, module_qn: str
    ) -> tuple[str, str, str]:
        # A local enum initialises when its method first uses it, so that
        # method runs the constructors: a dead method keeps them dead. Any
        # other enum initialises at class load, which the graph attributes to
        # the module, as it does a static field initializer.
        current = enum_node.parent
        while current is not None:
            if current.type in cs.JAVA_METHOD_NODE_TYPES:
                if loc := self._host.recorded_caller(current, module_qn):
                    return (loc.label, cs.KEY_QUALIFIED_NAME, loc.qualified_name)
                break
            current = current.parent
        return (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)

    def ingest_call_shape(self, ctx: CallScan, call_node: Node) -> bool:
        if call_node.type != cs.TS_JAVA_METHOD_REFERENCE:
            return False
        if (
            receiver := self.ingest_method_reference(ctx, call_node, ctx.ensure_rel)
        ) is not None:
            self._ingest_constructor_reference(ctx, call_node, receiver)
        return True

    def _ingest_constructor_reference(
        self, ctx: CallScan, ref_node: Node, receiver: Node
    ) -> None:
        # `Type::new` is a factory for Type: the type is resolved and
        # INSTANTIATES'd exactly as `new Type(...)` is, and its constructors are
        # referenced rather than called, since none runs here. The functional
        # interface that would pick one is not visible to the parser, so each
        # declared constructor is referenced, labelled `overload` when there
        # are several. An array type (`int[]::new`) constructs no class.
        if not (type_name := java_utils.method_reference_receiver_text(receiver)):
            return
        callee = ctx.resolve_func(
            type_name,
            ctx.module_qn,
            ctx.local_var_types,
            ctx.class_context,
            ctx.caller_qn,
            ctx.language,
            call_point=ref_node.start_byte,
            constructing=True,
        )
        if callee is None:
            self._host._note_unresolved(ctx.module_qn, written_simple_name(type_name))
            return
        if self._resolver.function_registry.get(callee[1]) == cs.NodeLabel.CLASS:
            self._host.construction.redirect_construction(
                ctx, callee[1], ctor_rel=cs.RelationshipType.REFERENCES
            )

    def resolve_callee(
        self,
        ctx: JavaCallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> Callee:
        if call_node.type != cs.TS_METHOD_INVOCATION:
            return UNDECIDED_CALLEE
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
                self._link_overload_ties(ctx, call_node, callee_info[1], ctx.ensure_rel)
        return Callee(True, callee_info)

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
        self._host.verdict = cs.EdgeResolution.OVERLOAD
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
            cs.EdgeResolution.OVERLOAD if len(edges) > 1 else self._host.verdict
        )
        for label, target_qn in edges:
            ensure_rel(
                ctx.caller_spec,
                cs.RelationshipType.REFERENCES,
                (label, cs.KEY_QUALIFIED_NAME, target_qn),
            )
        return None
