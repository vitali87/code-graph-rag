"""C#'s share of the call pass: the callee an invocation binds through the C#
engine, the type a target-typed `new()` constructs, the overload family a
method group hands over, the properties a caller reads (a property read is a
member access, never an invocation), and the namespace import each resolved
use pins.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver)
and hands C#'s part to the `CSharpCallResolution` it builds."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LanguageSpec
from ...node_text import decode_node_text, safe_decode_text
from ...types_defs import PropertyDict
from ..edge_site import EdgeSiteHost, host_site_scoped
from ..language_calls import LanguageCalls
from ..utils import module_qn_for_entity
from . import utils as csharp_utils
from .type_inference import CSHARP_EXTERNAL_TARGET, CSharpTypeInferenceEngine

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class CSharpCallHost(EdgeSiteHost, Protocol):
    """What C#'s call passes call on the call pass that builds them: the edge
    emitter, and the name-based resolve that notes its verdict for the call
    node being scanned."""

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def _resolve_noting(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
        constructing: bool = False,
    ) -> tuple[str, str] | None: ...


class CSharpCallScan(Protocol):
    """The caller a C# call node is scanned in."""

    caller_spec: tuple[str, str, str]
    caller_qn: str
    module_qn: str
    language: cs.SupportedLanguage
    class_context: str | None
    local_var_types: dict[str, str] | None


def member_access_call_name(func_child: Node) -> str | None:
    name_node = func_child.child_by_field_name(cs.FIELD_NAME)
    expr_node = func_child.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
    if name_node and name_node.text:
        method = decode_node_text(name_node.text)
        # A generic member (`recv.Handle<T>`) registers
        # generic-free; strip the type arguments so the
        # name-keyed fallbacks can match.
        method = method.split(cs.CHAR_ANGLE_OPEN, 1)[0]
        if expr_node and expr_node.text:
            receiver = decode_node_text(expr_node.text)
            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
        return method
    return None


def conditional_access_call_name(func_child: Node) -> str | None:
    binding = next(
        (
            child
            for child in func_child.children
            if child.type == cs.TS_CSHARP_MEMBER_BINDING_EXPRESSION
        ),
        None,
    )
    name_node = (
        binding.child_by_field_name(cs.FIELD_NAME) if binding is not None else None
    )
    if name_node and name_node.text:
        method = decode_node_text(name_node.text)
        receiver_node = (
            func_child.named_children[0] if (func_child.named_children) else None
        )
        if (
            receiver_node is not None
            and receiver_node is not binding
            and receiver_node.text
        ):
            receiver = decode_node_text(receiver_node.text)
            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
        return method
    return None


def target_typed_new_name(creation_node: Node) -> str | None:
    type_node = _target_type_node(creation_node)
    if type_node is None or type_node.text is None:
        return None
    return type_node.text.decode(cs.ENCODING_UTF8).split(cs.CHAR_ANGLE_OPEN, 1)[0]


def _target_type_node(creation_node: Node) -> Node | None:
    # The target type of a bare `new()` is named by the enclosing
    # declaration: a local/field `T x = new()` (initializer hangs directly
    # off the variable_declarator), a property initializer
    # `public T P { get; } = new()`, or a return position (`return new();`
    # / `=> new()`) typed by the enclosing member. Any other position (an
    # argument, an operand) needs overload resolution tree-sitter cannot
    # do: bail rather than guess.
    node = creation_node.parent
    while node is not None and node.type in (
        cs.TS_CSHARP_VARIABLE_DECLARATOR,
        cs.TS_CSHARP_EQUALS_VALUE_CLAUSE,
        cs.TS_PARENTHESIZED_EXPRESSION,
    ):
        node = node.parent
    if node is None:
        return None
    if node.type in (
        cs.TS_CSHARP_VARIABLE_DECLARATION,
        cs.TS_CSHARP_PROPERTY_DECLARATION,
    ):
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
    elif node.type in (
        cs.TS_RETURN_STATEMENT,
        cs.TS_CSHARP_ARROW_EXPRESSION_CLAUSE,
    ):
        type_node = _enclosing_return_type(node)
    else:
        return None
    if (
        type_node is None
        or type_node.text is None
        # `var x = new();` is ill-formed C# (no target type); if it
        # appears anyway, "var" is not a class name.
        or type_node.type == cs.TS_CSHARP_IMPLICIT_TYPE
    ):
        return None
    return type_node


def _enclosing_return_type(node: Node) -> Node | None:
    # A return position is typed by the nearest enclosing callable:
    # methods and local functions name it in `returns`, a property or
    # indexer (or their accessor bodies) in `type`. A lambda/anonymous
    # method carries no syntactic return type, so a `new()` returned from
    # one is unresolvable.
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in (
            cs.TS_CSHARP_METHOD_DECLARATION,
            cs.TS_CSHARP_LOCAL_FUNCTION_STATEMENT,
        ):
            return ancestor.child_by_field_name(cs.TS_CSHARP_FIELD_RETURNS)
        if ancestor.type in (
            cs.TS_CSHARP_PROPERTY_DECLARATION,
            cs.TS_CSHARP_INDEXER_DECLARATION,
        ):
            return ancestor.child_by_field_name(cs.FIELD_TYPE)
        if ancestor.type in cs.TS_CSHARP_NESTED_SCOPE_TYPES:
            return None
        ancestor = ancestor.parent
    return None


def _read_identifier(receiver: Node | None) -> str | None:
    # The identifier actually being READ in receiver position: unwrap
    # parens, a cast's VALUE (`((IDictionary<...>)WrappedDictionary)`),
    # and a null-forgiving postfix (`s!`) down to a bare identifier.
    while receiver is not None:
        if receiver.type == cs.TS_PARENTHESIZED_EXPRESSION:
            receiver = receiver.named_children[0] if receiver.named_children else None
            continue
        if receiver.type == cs.TS_CSHARP_CAST_EXPRESSION:
            receiver = receiver.child_by_field_name(cs.FIELD_VALUE)
            continue
        if receiver.type == cs.TS_CSHARP_POSTFIX_UNARY_EXPRESSION:
            receiver = receiver.named_children[0] if receiver.named_children else None
            continue
        break
    if receiver is not None and receiver.type == cs.TS_CSHARP_IDENTIFIER:
        return safe_decode_text(receiver)
    return None


def _shadow_spans(
    caller_node: Node,
    function_types: tuple[str, ...],
    class_types: tuple[str, ...],
) -> dict[str, list[tuple[int, int]]]:
    # {declared name: [byte span of each declaring SCOPE]} for every
    # declaration in the scopes the READ WALK descends into (locals
    # incl. untyped `var x = 3`, parameters, and a simple lambda's
    # bare implicit_parameter). A declaration shadows a same-name
    # property only for reads INSIDE its declaring scope: a lambda
    # param or a sibling block's local must not suppress an outer
    # read, while an in-lambda shadowed read must not fabricate a
    # property reference. The two walks skip the SAME nested
    # function/class scopes (each has its own pass).
    def scope_span(decl: Node) -> tuple[int, int]:
        anc = decl.parent
        while anc is not None and anc != caller_node:
            if anc.type in (cs.TS_CSHARP_BLOCK, cs.TS_CSHARP_LAMBDA_EXPRESSION):
                return (anc.start_byte, anc.end_byte)
            anc = anc.parent
        return (caller_node.start_byte, caller_node.end_byte)

    spans: dict[str, list[tuple[int, int]]] = {}
    stack = list(caller_node.children)
    while stack:
        node = stack.pop()
        node_type = node.type
        if node_type in function_types or node_type in class_types:
            continue
        name = None
        if node_type in (cs.TS_CSHARP_VARIABLE_DECLARATOR, cs.TS_CSHARP_PARAMETER):
            name = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
        elif node_type == cs.TS_CSHARP_IMPLICIT_PARAMETER:
            name = safe_decode_text(node)
        if name:
            spans.setdefault(name, []).append(scope_span(node))
        stack.extend(node.children)
    return spans


def _member_receiver_type(
    receiver: Node | None,
    recv_name: str | None,
    local_var_types: dict[str, str] | None,
) -> str | None:
    recv_type = None
    if recv_name:
        recv_type = (local_var_types or {}).get(recv_name, recv_name)
    elif (
        receiver is not None and receiver.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION
    ):
        dotted = safe_decode_text(receiver)
        if dotted and all(seg[:1].isupper() for seg in dotted.split(cs.SEPARATOR_DOT)):
            recv_type = dotted
    return recv_type


class CSharpCallResolution(LanguageCalls):
    """Binds C# invocations and object creations through the C# engine,
    writes the edges a method group or a property read makes, and pins the
    namespace import each resolved use reaches.

    Built and owned by `CallProcessor`. The resolver and the module paths are
    the pass's own, held by reference: the definition pass fills them as it
    registers each file."""

    __slots__ = ("_host", "_resolver", "_project_name", "_module_qn_to_file_path")

    # The argument-REFERENCE half without the callable-parameter flow,
    # which is untuned for C#: a method group passed as an argument
    # keeps its target reachable (Polly's EmptyHandler family).
    references_arguments = True
    # A class can still READ properties (`return Size;`) in a file with no
    # call expression.
    scans_callers_without_calls = True
    types_locals = True
    new_constructs_type = True

    def __init__(
        self,
        host: CSharpCallHost,
        resolver: CallResolver,
        project_name: str,
        module_qn_to_file_path: dict[str, Path],
    ) -> None:
        self._host = host
        self._resolver = resolver
        self._project_name = project_name
        self._module_qn_to_file_path = module_qn_to_file_path

    def function_field_name(self, func_child: Node) -> str | None:
        match func_child.type:
            case cs.TS_CSHARP_GENERIC_NAME:
                # Bare generic call `Handle<TException>(...)`: the callee
                # name is the identifier without its type arguments
                # (methods register generic-free). Leaving the `<...>` on
                # yielded no call name, so the call site vanished from the
                # graph (Polly's parameterless HandleInner overload
                # delegating to its Func sibling).
                if func_child.text is not None:
                    full = decode_node_text(func_child.text)
                    return full.split(cs.CHAR_ANGLE_OPEN, 1)[0]
            case cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
                # C# member call `recv.Method(...)`: emit the `recv.Method`
                # chain so the resolver can type `recv` and bind the method on
                # it; an unknown-type receiver falls back to the bare
                # method-name trie. `name` is the method, `expression` the
                # receiver.
                return member_access_call_name(func_child)
            case cs.TS_CSHARP_CONDITIONAL_ACCESS_EXPRESSION:
                # C# conditional call `recv?.Method(...)`: the method name
                # lives on the member_binding child. Emit the same
                # `recv.Method` chain as the unconditional form so the
                # resolver (or its exact Roslyn call fact) can bind it.
                return conditional_access_call_name(func_child)
        return None

    def node_call_name(self, call_node: Node) -> str | None:
        if call_node.type == cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION:
            # C# 9 target-typed `new()` has no `type` field; the
            # constructed type is named by the enclosing declaration
            # (issue #773).
            return target_typed_new_name(call_node)
        return None

    def record_cross_module_use(self, module_qn: str, entity_qn: str) -> None:
        # A resolved first-party entity pins the C# namespace import to the
        # module that defines it (issue #1347); external resolutions (stdlib,
        # builtins) never carry the project prefix and are skipped.
        if not entity_qn.startswith(f"{self._project_name}{cs.SEPARATOR_DOT}"):
            return
        target_module = module_qn_for_entity(entity_qn, self._module_qn_to_file_path)
        if target_module is not None and target_module != module_qn:
            self._resolver.import_processor.csharp.record_resolved_cross_module_use(
                module_qn, target_module
            )

    def resolve_callee(
        self,
        ctx: CSharpCallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
        ensure_rel: Callable[..., None],
        constructing: bool,
    ) -> tuple[str, str] | None:
        # `constructing`: the call node is an object creation, whose name the
        # name-based fallback reads as a TYPE (the call pass's own rule).
        callee_info = self._resolver.csharp.resolve_csharp_method_call(
            call_node, ctx.module_qn, call_var_types, ctx.caller_qn
        )
        if (
            callee_info is not None
            and callee_info != CSHARP_EXTERNAL_TARGET
            and (fn_node := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION))
            is not None
            and fn_node.type in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME)
        ):
            # A bare call resolved by ARITY may have same-arity
            # siblings differing only in parameter types (Serilog's
            # FormatExactNumericValue switch dispatch); keep the
            # whole family reachable. NOT when a Roslyn fact pinned
            # the site: that is the compiler's exact overload
            # choice, and widening it would revive dead siblings.
            engine = self._resolver.type_inference.csharp_type_inference
            if not engine.semantic_fact_resolved(call_node, ctx.module_qn):
                for sibling_qn in engine.csharp_same_arity_family(callee_info[1]):
                    ensure_rel(
                        ctx.caller_spec,
                        cs.RelationshipType.CALLS,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, sibling_qn),
                    )
        if callee_info == CSHARP_EXTERNAL_TARGET:
            # Provably external (base.X() with an external base, a
            # static call on an unregistered type, an object
            # virtual on an untyped receiver): no edge, and no
            # name-trie fallback that would fabricate one onto an
            # unrelated same-name first-party member.
            callee_info = None
        elif callee_info is None and (
            (fn_node := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)) is None
            or fn_node.type not in (cs.TS_CSHARP_IDENTIFIER, cs.TS_CSHARP_GENERIC_NAME)
        ):
            # An untyped C# MEMBER call retains the generic fallback. A
            # bare call has already exhausted C#'s legitimate scopes
            # (local function, enclosing type, and static import), so a
            # miss is external rather than a name-wide guess (#2005).
            callee_info = self._host._resolve_noting(
                call_name,
                ctx.module_qn,
                call_var_types,
                ctx.class_context,
                ctx.caller_qn,
                ctx.language,
                constructing=constructing,
            )
        return callee_info

    def construction_by_arity(
        self,
        creation_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        # The name lookup ignores type arguments, so `new PB<int>(...)` and
        # `new PB(...)` land on one of the twins `PB` and `PB<TResult>`; the
        # written arity moves it to the type C# constructs (issue #2579).
        if callee_info is None or callee_info[0] != cs.NodeLabel.CLASS:
            return callee_info
        engine = self._resolver.type_inference.csharp_type_inference
        if creation_node.type == cs.TS_OBJECT_CREATION_EXPRESSION:
            type_node = creation_node.child_by_field_name(cs.FIELD_TYPE)
        elif (type_node := _target_type_node(creation_node)) is None:
            # An argument's `new(...)`, typed by a parameter whose signature
            # spells no arity: a name with a twin of another arity stays
            # unresolved rather than guessed.
            if engine.arity_index().has_twin(callee_info[1]):
                return None
            return callee_info
        if type_node is None or not (written := safe_decode_text(type_node)):
            return callee_info
        # Only a written path the call name spells whole: `new Outer<int>.Inner()`
        # is looked up as `Outer`, whose arity the text does not give.
        if csharp_utils.normalize_csharp_type_name(type_node) != call_name:
            return callee_info
        arity = csharp_utils.generic_arity_of_type_text(written)
        return callee_info[0], engine.arity_twin(callee_info[1], arity)

    def fallback_call_name(self, ctx: CSharpCallScan, call_node: Node) -> str | None:
        # `Use(new(4))`: an argument's `new(...)` constructs the type of the
        # parameter it binds, which only the resolved callee can say
        # (issue #2579).
        if call_node.type != cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION:
            return None
        argument = call_node.parent
        while argument is not None and argument.type == cs.TS_PARENTHESIZED_EXPRESSION:
            argument = argument.parent
        if argument is None or argument.type != cs.TS_CSHARP_ARGUMENT:
            return None
        engine = self._resolver.type_inference.csharp_type_inference
        return engine.argument_parameter_type(
            argument, ctx.local_var_types, ctx.module_qn, ctx.caller_qn
        )

    def emit_method_group(
        self,
        arg_node: Node,
        arg_text: str,
        source_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        caller_qn: str | None,
        ensure_rel: Callable[..., None],
        rel_type: cs.RelationshipType,
    ) -> bool:
        # True when the C# argument was handled as a method group (edges
        # emitted, or deliberately none); False falls through to resolution.
        # A bare method-group name binds the ENCLOSING type's method
        # group; reference the whole overload family (the delegate
        # type that selects one overload is invisible to syntax, and
        # the trie's lexicographic pick can even land on a sibling
        # class, Polly's EmptyAction). Falls through when the enclosing
        # type has no such member.
        if cs.SEPARATOR_DOT not in arg_text:
            engine = self._resolver.type_inference.csharp_type_inference
            # An in-scope LOCAL FUNCTION shadows any same-name member
            # (C# scoping) and the trie cannot see it at all: reference
            # the local group first (Serilog's CreateLogger passes its
            # Dispose/DisposeAsync locals to the Logger ctor).
            if locals_group := engine.csharp_local_function_group(
                arg_text, caller_qn, module_qn
            ):
                for target_qn in locals_group:
                    ensure_rel(
                        source_spec,
                        rel_type,
                        (
                            cs.NodeLabel.FUNCTION,
                            cs.KEY_QUALIFIED_NAME,
                            target_qn,
                        ),
                    )
                return True
            if family := engine.csharp_method_group_family(arg_text, caller_qn):
                for target_qn in family:
                    ensure_rel(
                        source_spec,
                        rel_type,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, target_qn),
                    )
                    self.record_cross_module_use(module_qn, target_qn)
                return True
        else:
            # `recv.Name` is a method group only on a receiver the engine
            # can type; nothing else about it reaches the simple-name
            # fallback, which bound an untyped `@override.Value` to a
            # same-named first-party property (issue #1998).
            engine = self._resolver.type_inference.csharp_type_inference
            for target_qn in engine.csharp_member_group_argument(
                arg_node, local_var_types or {}, module_qn, caller_qn
            ):
                ensure_rel(
                    source_spec,
                    rel_type,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, target_qn),
                )
                self.record_cross_module_use(module_qn, target_qn)
            return True
        return False

    @host_site_scoped
    def ingest_property_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
    ) -> None:
        # A property getter access is a member_access_expression (usually in
        # RECEIVER position), never an invocation, so callers that only READ
        # a property emit no edge to it and dead-code flags it (Polly's
        # Context.WrappedDictionary, ResiliencePipeline<T>.Pipeline).
        # Emit a REFERENCES edge (a read is not an invocation; the call
        # graph stays invocation-only) from the caller to each property of
        # its enclosing type read in receiver position. Registry-guarded via
        # resolve_property_read, which accepts only marked properties.
        engine = self._resolver.type_inference.csharp_type_inference
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        shadow_cell: list[dict[str, list[tuple[int, int]]]] = []
        seen: set[str] = set()

        def shadow_spans() -> dict[str, list[tuple[int, int]]]:
            # Built lazily on the first candidate read; most callers have none.
            if not shadow_cell:
                shadow_cell.append(
                    _shadow_spans(caller_node, function_types, class_types)
                )
            return shadow_cell[0]

        def try_emit(name: str | None, read_node: Node, this_read: bool) -> None:
            self._try_emit_read(
                name,
                read_node,
                this_read,
                engine,
                caller_spec,
                caller_qn,
                module_qn,
                prop_names,
                seen,
                shadow_spans,
            )

        stack = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
                self._member_access_reads(
                    node,
                    try_emit,
                    engine,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    prop_names,
                    seen,
                )
            elif node_type == cs.TS_CSHARP_IDENTIFIER:
                # A bare identifier expression (`return Size;`, `Use(Size)`,
                # `var n = Size;`) reads the getter just the same. NOT a
                # read: any member-access position (receiver/name handled
                # above), a parent's NAME field (a declarator/parameter's
                # own name, a named-argument label `Use(Size: 3)`), or a
                # parent's TYPE field (`new Size()`, `(Size)x`).
                # `==`, not `is`: child_by_field_name returns a fresh Node
                # wrapper each call, so identity never matches.
                parent = node.parent
                if (
                    parent is not None
                    and parent.type != cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION
                    and parent.child_by_field_name(cs.FIELD_NAME) != node
                    and parent.child_by_field_name(cs.FIELD_TYPE) != node
                ):
                    try_emit(safe_decode_text(node), node, False)
            stack.extend(node.children)

    def _try_emit_read(
        self,
        name: str | None,
        read_node: Node,
        this_read: bool,
        engine: CSharpTypeInferenceEngine,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        prop_names: set[str],
        seen: set[str],
        shadow_spans: Callable[[], dict[str, list[tuple[int, int]]]],
    ) -> None:
        # One candidate read of an enclosing-type property; a bare read a local
        # declaration shadows is not the member, a `this.`-qualified one always is.
        if not name or name not in prop_names or name in seen:
            return
        shadowed = shadow_spans()
        pos = read_node.start_byte
        is_shadowed = any(lo <= pos < hi for lo, hi in shadowed.get(name, ()))
        if (this_read or not is_shadowed) and (
            prop_qn := engine.resolve_property_read(name, caller_qn)
        ):
            seen.add(name)
            if prop_qn != caller_qn:
                self._host._emit_rel(
                    caller_spec,
                    cs.RelationshipType.REFERENCES,
                    (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, prop_qn),
                )
                self.record_cross_module_use(module_qn, prop_qn)

    def _member_access_reads(
        self,
        node: Node,
        try_emit: Callable[[str | None, Node, bool], None],
        engine: CSharpTypeInferenceEngine,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        prop_names: set[str],
        seen: set[str],
    ) -> None:
        receiver = node.child_by_field_name(cs.TS_CSHARP_FIELD_EXPRESSION)
        # `this.Size` is ALWAYS the member (a local can never
        # shadow a this-qualified read), so the read is the NAME
        # field and the shadow check does not apply.
        this_read = receiver is not None and receiver.type == cs.TS_CSHARP_THIS
        if this_read:
            try_emit(
                safe_decode_text(node.child_by_field_name(cs.FIELD_NAME)),
                node,
                True,
            )
        else:
            recv_name = _read_identifier(receiver)
            try_emit(recv_name, node, False)
            # The NAME field can be a property of the RECEIVER's
            # type: `Cfg.Value` (static, class-name receiver),
            # `w.Inner` (instance, local of inferred type), or
            # `N.Cfg.Value` (namespace/type-qualified: a dotted
            # receiver of all-PascalCase segments names a type, a
            # camelCase head is an expression chain and is skipped).
            # An unresolvable receiver yields nothing, so unrelated
            # chains never fabricate an edge.
            member = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
            recv_type = _member_receiver_type(receiver, recv_name, local_var_types)
            if recv_type and member and member in prop_names:
                prop_qn = engine.resolve_member_property_read(
                    recv_type, member, module_qn
                )
                if prop_qn is not None and prop_qn != caller_qn and prop_qn not in seen:
                    seen.add(prop_qn)
                    self._host._emit_rel(
                        caller_spec,
                        cs.RelationshipType.REFERENCES,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, prop_qn),
                    )
