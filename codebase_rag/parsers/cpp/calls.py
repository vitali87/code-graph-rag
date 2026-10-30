"""C++'s share of the call pass: the overload an operator expression runs,
the functor a call or a passed object reaches, the class a body-local alias
or type names, and the constructions and lifecycle calls C++ writes without
a call expression (a braced return, a member initializer, a declaration, a
base class's implicit constructor and destructor). And C's: the functions a
C file stores as values, bound by C's scoping and linkage.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver)
and hands C++'s part to the `CppCallResolution` it builds, C's to the
`CCallResolution`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...node_text import decode_node_text, safe_decode_text
from ...types_defs import LanguageQueries
from ...utils import qn_markers
from ..call_host import CallbackEdgeHost, RecordedCallerHost, ResolvedCallHost
from ..callable_params import cpp_parameter_names
from ..definition_facts import DefinitionFacts
from ..edge_site import EdgeWriterHost, host_site_scoped
from ..language_calls import CallScan, LanguageCalls
from ..scope_boundaries import flow_scope_boundaries
from . import local_types as cpp_local_types
from . import utils as cpp_utils
from .function_values import (
    CFileScope,
    c_file_scope,
    c_function_value_identifiers,
    local_enumerator_names,
)
from .type_inference import CppTypeInferenceEngine

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class CppCallHost(
    EdgeWriterHost, RecordedCallerHost, CallbackEdgeHost, ResolvedCallHost, Protocol
):
    """What C++ call resolution calls on the call pass that builds it: the
    edge emitter, the location the definition pass recorded for a function
    node, the binding, whose ingest of a call bound to its callee writes the
    edges every resolved call writes, and the callback edges a passed
    functor's reference goes through like any other callback's."""


class CppCallScan(Protocol):
    """The caller a C++ call node is scanned in, and the maps built on it
    once, on first use, for all of that caller's calls."""

    caller_node: Node
    caller_spec: tuple[str, str, str]
    module_qn: str
    class_context: str | None
    local_var_types: dict[str, str] | None
    cpp_template_params: frozenset[str]
    ensure_rel: Callable[..., None]
    cpp_local_aliases: dict[str, list[tuple[str, int, int]]] | None
    cpp_indirection: dict[str, int] | None


# The node kinds a C++ type written in a function body registers as.
_LOCAL_TYPE_NODE_TYPES = frozenset(
    {cs.NodeLabel.CLASS, cs.NodeLabel.UNION, cs.NodeLabel.ENUM}
)
# C/C++ expression nodes whose call name is a synthesized operator_*, the
# ones whose operand type may direct or suppress the binding.
OPERATOR_EXPRESSION_TYPES = frozenset(
    {
        cs.TS_CPP_BINARY_EXPRESSION,
        cs.TS_CPP_UNARY_EXPRESSION,
        cs.TS_CPP_UPDATE_EXPRESSION,
    }
)


def _without_parentheses(operand: Node | None) -> Node | None:
    """`(a)` and `((a))` as `a`."""
    while operand is not None and operand.type == cs.TS_PARENTHESIZED_EXPRESSION:
        operand = operand.named_children[0] if operand.named_children else None
    return operand


def _is_dereference(operand: Node) -> bool:
    """Whether `operand` is `*p`, as opposed to another pointer expression (`&a`)."""
    return (
        operand.type == cs.TS_CPP_POINTER_EXPRESSION
        and safe_decode_text(operand.child_by_field_name(cs.FIELD_OPERATOR))
        == cs.CPP_DEREFERENCE
    )


def _scoped_bindings(
    scopes: list[tuple[int, int, str, bool]],
    name: str,
    type_name: str,
    local_qn: str,
) -> list[tuple[int, int, str, str]]:
    # Each C++ declaration of `name` as a span binding, widest first: a
    # qualified one keeps the bare type name, which the module-wide lookup
    # resolves to the class the qualifier names; an unqualified one binds
    # the local type.
    bindings = [
        (start, end, name, type_name if qualified else local_qn)
        for start, end, scope_name, qualified in scopes
        if scope_name == name
    ]
    bindings.sort(key=lambda binding: binding[0] - binding[1])
    return bindings


def types_at(
    var_types: dict[str, str], spans: list[tuple[int, int, str, str]], pos: int
) -> dict[str, str]:
    # The type map as a C++ use at byte `pos` sees it: each spanned name
    # bound by the narrowest of its declarations whose window holds `pos`.
    narrowest: dict[str, tuple[int, str]] = {}
    for start, end, name, bound in spans:
        best = narrowest.get(name)
        if start <= pos < end and (best is None or end - start < best[0]):
            narrowest[name] = (end - start, bound)
    return {**var_types, **{name: bound for name, (_, bound) in narrowest.items()}}


def _declaration_is_construction(node: Node) -> bool:
    # `Point origin(10, 10)`: an init_declarator carrying a direct
    # argument list; otherwise the most-vexing-parse evidence test.
    for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR):
        if declarator.type == cs.CppNodeType.INIT_DECLARATOR and any(
            child.type == cs.TS_ARGUMENT_LIST for child in declarator.children
        ):
            return True
    return cpp_utils.is_cpp_vexing_parse_construction(node)


def _declaration_type_name(node: Node) -> str | None:
    # The written type spelling, normalized like a member-init head:
    # registered class qns are unspecialized and dot-separated, so cut
    # at the first `<` and normalize `::`.
    type_node = node.child_by_field_name(cs.FIELD_TYPE)
    if type_node is None or type_node.text is None:
        return None
    name = type_node.text.decode(cs.ENCODING_UTF8).split(cs.CHAR_ANGLE_OPEN, 1)[0]
    return name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT) or None


def _member_init_head_name(initializer: Node) -> str | None:
    # The head is the initializer's first named child: a plain
    # field_identifier (`buffer`), a template_method (`base<T>`), or a
    # qualified_identifier (`ns::other`, `ns::base<int>` -- the
    # qualified node CONTAINS the template one, so branching on the
    # outer type cannot strip the specialization args). The raw text
    # carries the written spelling in every shape; registered class qns
    # are unspecialized and dot-separated, so cut at the first `<` and
    # normalize `::` (PR #792 review: `ns::base<int>(g)` resolved
    # nothing because the args leaked into the lookup).
    head = next(iter(initializer.named_children), None)
    if (
        head is None
        or head.type
        not in (
            cs.CppNodeType.FIELD_IDENTIFIER,
            cs.CppNodeType.TEMPLATE_METHOD,
            cs.CppNodeType.QUALIFIED_IDENTIFIER,
        )
        or head.text is None
    ):
        return None
    name = head.text.decode(cs.ENCODING_UTF8).split(cs.CHAR_ANGLE_OPEN, 1)[0]
    return name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT) or None


def field_call_name(func_child: Node) -> str | None:
    field_node = func_child.child_by_field_name(cs.FIELD_FIELD)
    if field_node and field_node.text:
        method = decode_node_text(field_node.text)
        # Prepend a simple-identifier receiver (`obj->m`/`obj.m`
        # -> `obj.m`) so the resolver can map obj to its type and
        # bind the correct class method; a `.`-joined two-part name
        # falls back to the bare method-name trie when the receiver
        # type is unknown. Complex receivers (chains, calls, `this`)
        # keep the bare method name.
        arg = func_child.child_by_field_name(cs.TS_FIELD_ARGUMENT)
        if arg is not None and arg.type == cs.TS_IDENTIFIER and arg.text:
            receiver = decode_node_text(arg.text)
            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
        # A factory-call receiver (`parser(ia, cb).parse(...)`,
        # nlohmann's basic_json::parse) is a call_expression on a
        # bare identifier: emit the chain form so the resolver can
        # type the factory's return and bind the method on it. A
        # template/qualified callee (`Reader<T>(...)`,
        # `detail::Reader<T>(...)`) is a CONSTRUCTOR TEMPORARY: the
        # callee names the receiver's class directly. Deeper receiver
        # chains keep the bare method-name trie fallback.
        if (
            arg is not None
            and arg.type == cs.TS_CPP_CALL_EXPRESSION
            and (callee := arg.child_by_field_name(cs.TS_FIELD_FUNCTION)) is not None
            and callee.type
            in (
                cs.TS_IDENTIFIER,
                cs.TS_CPP_TEMPLATE_FUNCTION,
                cs.TS_CPP_QUALIFIED_IDENTIFIER,
            )
            and arg.text
        ):
            receiver = decode_node_text(arg.text)
            return f"{receiver}{cs.SEPARATOR_DOT}{method}"
        return method
    return None


class CppCallResolution(LanguageCalls):
    """Binds the C++ calls a name lookup cannot, and writes the constructions
    and lifecycle calls C++ runs without a call expression.

    Built and owned by `CallProcessor`. The resolver and the definition facts
    are the pass's own, held by reference: the definition pass fills them as
    it registers each file."""

    __slots__ = ("_host", "_resolver", "_facts")

    flows_callables = True
    references_arguments = True
    # A constructor's member initializer list (`: buffer(g, 0)`) runs base
    # constructors without any call_expression node.
    scans_callers_without_calls = True
    types_locals = True
    binds_operator_calls = True
    runs_declared_constructors = True

    def __init__(
        self, host: CppCallHost, resolver: CallResolver, facts: DefinitionFacts
    ) -> None:
        self._host = host
        self._resolver = resolver
        self._facts = facts

    def caller_name(self, func_node: Node, module_qn: str) -> str | None:
        # A macro-invocation artifact the ingest pass declined to register
        # (no class bears its name) must not become a call target either;
        # mirror ingest's decision via the recorded locations so the two
        # passes never diverge.
        if (
            cpp_utils.is_macro_invocation_artifact(func_node)
            and self._host.recorded_caller(func_node, module_qn) is None
        ):
            return None
        # C and C++ share the function_definition/declarator shape, so the
        # name lives in a nested declarator (no `name` field), needing the
        # declarator-aware extractor rather than the `name` field.
        return cpp_utils.extract_function_name(func_node) or None

    def method_name(self, method_node: Node, module_qn: str) -> str | None:
        return cpp_utils.extract_function_name(method_node)

    def class_name(self, class_node: Node) -> str | None:
        # A specialization is spelled as the definition pass spells it,
        # so a caller qn built from it names the same class (issue #2434).
        name_node = class_node.child_by_field_name(cs.FIELD_NAME)
        return cpp_utils.type_name_text(name_node) if name_node is not None else None

    def node_call_name(self, call_node: Node) -> str | None:
        if call_node.type != cs.TS_NEW_EXPRESSION:
            return None
        # `new Foo(...)` names the class via the `type` field (no
        # `function` field), so it fell through nameless and heap
        # construction emitted nothing (issue #896, the singleton
        # `new WindowClassRegistrar()`). Returning the type name routes
        # it through the normal resolve loop: the class gets
        # INSTANTIATES and its constructor CALLS. The type reduces
        # STRUCTURALLY to its dotted class path (`new Foo<int>()` ->
        # Foo, `new Outer<int>::Inner()` -> Outer.Inner; a textual cut
        # at `<` would drop the nested suffix); a primitive type
        # reduces to nothing and stays silent.
        type_node = call_node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None:
            return None
        return cpp_utils.new_expression_class_path(type_node)

    def class_qn(self, class_node: Node, module_qn: str, class_name: str) -> str | None:
        # A class inside a namespace is bound through the namespaces around
        # it (qn `module.ns.Class`).
        return cpp_utils.build_qualified_name(class_node, module_qn, class_name)

    def function_qn(
        self, func_node: Node, module_qn: str, func_name: str
    ) -> str | None:
        # A free function inside a namespace is bound by the definition
        # pass via build_qualified_name (qn `module.ns.fn`); the nested-scope
        # builder ignores namespace_definition ancestors and would drop the
        # namespace (`module.fn`), dangling the CALLS source.
        return cpp_utils.build_qualified_name(func_node, module_qn, func_name)

    def parameter_names(self, caller_node: Node) -> list[str] | None:
        return cpp_parameter_names(caller_node)

    def ingest_caller_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        self.ingest_braced_return_instantiations(
            caller_node, caller_spec, caller_qn, module_qn
        )
        self.ingest_member_init_ctor_calls(caller_node, caller_spec, module_qn)
        self.ingest_implicit_base_lifecycle_calls(
            caller_node, caller_spec, caller_qn, module_qn
        )
        self.ingest_declaration_ctor_calls(caller_node, caller_spec, module_qn)

    def caller_calls(self, func_node: Node, calls: list[Node]) -> list[Node]:
        # A member function of a type written in this body is its own
        # caller; its calls are not also this function's (issue #2555).
        # A lambda is no caller of its own, so its calls still count here.
        return [
            call
            for call in calls
            if not cpp_local_types.in_local_type_member(call, func_node)
        ]

    def bound_method_caller(
        self, func_node: Node, func_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # Resolve an out-of-line C++ method definition (`Ret Class::method()
        # {...}` at namespace/file scope) to its (method_qn, class_qn),
        # mirroring the definition pass's class binding. The leaf class name
        # resolves the class across files (header-declared classes);
        # `endswith(normalized)` guards against a leaf collision binding to the
        # wrong class, and the registry check ensures the method node exists
        # before overriding the default attribution.
        if not cpp_utils.is_out_of_class_method_definition(func_node):
            return None
        # The definition pass already bound this exact definition (keyed by
        # module + start line); reuse its decision so the caller qn matches
        # the registered Method node by construction.
        recorded = self._facts.cpp_out_of_class_methods.get(
            (module_qn, func_node.start_point[0] + 1)
        )
        if recorded is not None:
            method_qn, class_qn = recorded
            if method_qn in self._resolver.function_registry:
                return method_qn, class_qn
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_node)
        if not class_name:
            return None
        normalized = class_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
        leaf = normalized.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        class_qn = self._resolver.resolve_class_name(leaf, module_qn)
        if not class_qn or not class_qn.endswith(normalized):
            return None
        caller_qn = f"{class_qn}{cs.SEPARATOR_DOT}{func_name}"
        if caller_qn in self._resolver.function_registry:
            return caller_qn, class_qn
        return None

    def local_type_spans(
        self,
        caller_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> list[tuple[int, int, str, str]]:
        if not local_var_types:
            return []
        # A variable of a type written in an enclosing function body binds
        # to that type's node, not whichever same-named class the
        # module-wide lookup meets first, so two functions' local
        # `Checker`s stay apart (issue #2555). A position name that binds to
        # nothing names a type with no node, and must not reach the
        # name-based lookups.
        # A name declared with a qualified type (`::B g`, `ns::B n`) keeps
        # the class its qualifier names (#2631). Where a name's
        # declarations disagree (a block local `B b` hiding a `::B b`
        # field), the widest one is the flat binding and each is returned
        # as a span binding over its window, so a call binds to the
        # declaration in force where it sits.
        anchors = self._local_anchors(caller_node, module_qn)
        scopes: list[tuple[int, int, str, bool]] | None = None
        spans: list[tuple[int, int, str, str]] = []
        for name, type_name in list(local_var_types.items()):
            if local_qn := self._local_type_under(anchors, type_name):
                if scopes is None:
                    scopes = CppTypeInferenceEngine().declaration_scopes(caller_node)
                bindings = _scoped_bindings(scopes, name, type_name, local_qn)
                local_var_types[name] = bindings[0][3] if bindings else local_qn
                if any(binding[3] != local_var_types[name] for binding in bindings):
                    spans.extend(bindings)
            elif cpp_local_types.is_positional_name(type_name):
                del local_var_types[name]
        return spans

    def _local_anchors(self, node: Node, module_qn: str) -> list[str]:
        # The qns the types written in each function and local type around
        # `node` are named under, innermost first.
        return cpp_local_types.scope_anchor_qns(
            node, module_qn, self._facts.function_locations
        )

    def _local_type_under(self, anchors: list[str], type_name: str) -> str | None:
        registry = self._resolver.function_registry
        for anchor in anchors:
            local_qn = f"{anchor}{cs.SEPARATOR_DOT}{type_name}"
            if registry.get(local_qn) in _LOCAL_TYPE_NODE_TYPES:
                return local_qn
        return None

    def template_params(self, caller_node: Node) -> frozenset[str]:
        # `template<typename SAX>` -> {"SAX"}.
        return self._resolver.type_inference.cpp_type_inference.collect_template_param_names(
            caller_node
        )

    def ingest_functor_call(self, ctx: CallScan, call_node: Node) -> bool:
        # `enter_state(n)` on a functor local, `Cmp{}(a, b)` on a temporary:
        # a call on an object of class type runs that type's operator(),
        # which no name lookup of the callee text reaches (issue #2555).
        if (
            call_node.type == cs.TS_CPP_CALL_EXPRESSION
            and (callee := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION))
            and (
                functor := self.functor_operator(
                    callee, ctx.module_qn, ctx.local_var_types
                )
            )
        ):
            self._host.binding.ingest_resolved_call(
                ctx, call_node, cs.CPP_OPERATOR_CALL_NAME, functor
            )
            return True
        return False

    def ingest_own_call(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> bool:
        self.emit_template_dispatch(
            ctx.caller_spec,
            call_name,
            call_var_types,
            ctx.cpp_template_params,
            ctx.ensure_rel,
        )
        if not (
            call_node.type in OPERATOR_EXPRESSION_TYPES
            and call_name.startswith(cs.OPERATOR_PREFIX)
        ):
            return False
        # An operator expression calls an overload only through an
        # operand of class type; on built-in or unknown operands it is
        # the language's own operation. Resolving its synthesized name
        # like a call bound `int + int` to any project operator+, and,
        # in the class defining one, its own `x + o.x` field arithmetic
        # to itself (issue #2554). A miss notes no unresolved name:
        # most C++ projects define some operator_plus, so every file
        # using `+` would re-parse whenever another one is added.
        if callee_info := self.operator_callee(
            ctx, call_node, call_name, call_var_types
        ):
            self._host.binding.ingest_resolved_call(
                ctx, call_node, call_name, callee_info
            )
        return True

    def emit_template_dispatch(
        self,
        caller_spec: tuple[str, str, str],
        call_name: str,
        call_var_types: dict[str, str] | None,
        template_params: frozenset[str],
        ensure_rel: Callable[..., None],
    ) -> None:
        # A C++ member call through a template-parameter receiver has no
        # concrete type, so precise resolution fails and the external-receiver
        # guard drops the edge, orphaning every structural interface
        # implementer (json_sax_* visitors dispatched via `sax->start_object()`).
        # Fan such a call out to the method on every class defining it. Runs
        # before the call pass's primary resolution so it fires even when
        # that edge is dropped; a concretely-typed receiver is skipped inside
        # the resolver. sorted(): the target label is a hash-randomized
        # StrEnum, so sort for deterministic output.
        for target_type, target_qn in sorted(
            self._resolver.cpp.cpp_dispatch_targets(
                call_name, call_var_types, template_params
            )
        ):
            for variant in self._resolver.function_registry.variants(target_qn):
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (target_type, cs.KEY_QUALIFIED_NAME, variant),
                )

    def operator_callee(
        self,
        ctx: CppCallScan,
        call_node: Node,
        call_name: str,
        var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        # The left operand (the sole one of a unary/update op) selects a
        # member or a free overload. The right one selects only a free
        # overload (`os << v`, `2 * v`): a member operator takes its own
        # class on the left. A free overload takes every operand as a
        # parameter, plus the dummy `int` that marks a postfix `v++`.
        if call_node.type == cs.TS_CPP_BINARY_EXPRESSION:
            operands = (
                call_node.child_by_field_name(cs.FIELD_LEFT),
                call_node.child_by_field_name(cs.FIELD_RIGHT),
            )
            arity = 2
        else:
            argument = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENT)
            operator = call_node.child_by_field_name(cs.FIELD_OPERATOR)
            operands = (argument,)
            arity = (
                2
                if argument is not None
                and operator is not None
                and operator.start_byte > argument.start_byte
                else 1
            )
        resolver = self._resolver
        for position, operand in enumerate(operands):
            if (class_qn := self._operand_class(ctx, operand, var_types)) is None:
                continue
            if position == 0 and (
                member := resolver.cpp.cpp_member_operator(call_name, class_qn)
            ):
                return member
            if free := resolver.cpp.cpp_free_operator_for_type(
                call_name, class_qn, position, arity
            ):
                return free
        return None

    def _operand_class(
        self,
        ctx: CppCallScan,
        operand: Node | None,
        var_types: dict[str, str] | None,
    ) -> str | None:
        # The registered class an operand is a value of: a local or
        # parameter of that type (not a pointer or array of it), `*p` on a
        # single pointer to it, or `*this`. A field, a call result or a
        # literal is never typed here, so it selects no overload.
        operand = _without_parentheses(operand)
        depth = 0
        if operand is not None and _is_dereference(operand):
            operand = operand.child_by_field_name(cs.TS_FIELD_ARGUMENT)
            depth = 1
            if operand is not None and operand.type == cs.CppNodeType.THIS:
                return self._this_class(ctx)
        if operand is None or operand.type != cs.TS_IDENTIFIER:
            return None
        return self._variable_class(ctx, operand, depth, var_types)

    def _this_class(self, ctx: CppCallScan) -> str | None:
        # `*this`: the enclosing class, when it is a registered one.
        class_qn = ctx.class_context
        if class_qn and class_qn in self._resolver.function_registry:
            return class_qn
        return None

    def _variable_class(
        self,
        ctx: CppCallScan,
        identifier: Node,
        depth: int,
        var_types: dict[str, str] | None,
    ) -> str | None:
        # A variable's class, when it is reached through exactly `depth`
        # pointers: `a` for a value, `*p` for a single pointer.
        name = safe_decode_text(identifier)
        if ctx.cpp_indirection is None:
            ctx.cpp_indirection = CppTypeInferenceEngine().build_indirection_map(
                ctx.caller_node
            )
        if name is None or ctx.cpp_indirection.get(name, 0) != depth:
            return None
        return self._resolver.cpp.cpp_operand_class_qn(name, var_types, ctx.module_qn)

    def local_alias_callee(
        self, ctx: CppCallScan, call_node: Node, call_name: str
    ) -> tuple[str, str] | None:
        # `using appender = basic_appender<char>; appender(out)`:
        # the alias is no registered node, so the bare call resolves
        # to nothing and the constructed class's ctor stays edge-free.
        # The cross-file typedef/using map covers file/namespace-scope
        # aliases; a BODY-local alias is what that collector skips, so
        # it comes from the caller-scoped map (`resolve_class_name` then
        # follows any further alias chain). Binding the callee to the
        # class drops into the call pass's class branch, emitting INSTANTIATES
        # + ctor CALLS like any construction. Gated on alias-map
        # membership so genuinely unknown names keep their unresolved
        # handling.
        if ctx.cpp_local_aliases is None:
            ctx.cpp_local_aliases = CppTypeInferenceEngine().collect_local_type_aliases(
                ctx.caller_node
            )
        lookup_name = call_name
        # Declaration-ordered AND lexically-scoped lookup: a call
        # BEFORE the body-local alias's declaration or AFTER its
        # enclosing block/lambda closes can never mean it; among
        # windows that do contain the call, the latest declaration
        # wins (C++ shadowing).
        best_decl_end = -1
        for underlying, decl_end, scope_end in ctx.cpp_local_aliases.get(call_name, ()):
            if (
                decl_end <= call_node.start_byte < scope_end
                and decl_end > best_decl_end
            ):
                lookup_name = underlying
                best_decl_end = decl_end
        if (
            lookup_name != call_name or call_name in self._resolver.facts.type_aliases
        ) and (
            aliased_qn := self._resolver.resolve_class_name(lookup_name, ctx.module_qn)
        ):
            return (cs.NodeLabel.CLASS, aliased_qn)
        return None

    def bound_callee(
        self,
        ctx: CppCallScan,
        call_node: Node,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> tuple[str, str]:
        if call_node.type == cs.TS_NEW_EXPRESSION and callee_type != cs.NodeLabel.CLASS:
            return self.rebind_new_callee(ctx, call_name, callee_type, callee_qn)
        return callee_type, callee_qn

    def rebind_new_callee(
        self, ctx: CppCallScan, call_name: str, callee_type: str, callee_qn: str
    ) -> tuple[str, str]:
        # Inside X's own methods the bare name `X` prefers the class
        # MEMBER (the ctor), so `new X()` resolves past the class
        # branch and INSTANTIATES is lost (issue #896, the singleton's
        # `new WindowClassRegistrar()` in GetInstance). Construction
        # always targets the CLASS: rebind so the call pass's class branch
        # emits INSTANTIATES plus the full ctor/dtor redirect.
        leaf = call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        class_qn = self._resolver.resolve_class_name(leaf, ctx.module_qn)
        if class_qn is not None and (
            class_qn == call_name or class_qn.endswith(f"{cs.SEPARATOR_DOT}{call_name}")
        ):
            return (cs.NodeLabel.CLASS, class_qn)
        return (callee_type, callee_qn)

    def emit_functor_callback(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        ensure_rel: Callable[..., None],
    ) -> bool:
        # A functor handed over (`std::sort(b, e, Cmp{})`) keeps its
        # operator() reachable (issue #2555). The object is passed as a
        # value: whether the callee runs it or only stores it
        # (`values.push_back(f)`) is not visible here, so the pass is a
        # REFERENCES edge, never an invocation (#2631). True when the
        # argument was one and its edge is out.
        functor = self.functor_operator(arg_node, module_qn, local_var_types)
        if functor is None:
            return False
        self._host.callbacks.emit_callback_targets(
            source_spec,
            functor[0],
            functor[1],
            cs.RelationshipType.REFERENCES,
            ensure_rel,
            module_qn,
            cs.SupportedLanguage.CPP,
            cs.EdgeResolution.EXACT,
        )
        return True

    def functor_operator(
        self, node: Node, module_qn: str, var_types: dict[str, str] | None
    ) -> tuple[str, str] | None:
        # The operator() an object-valued expression runs when it is called:
        # a local or parameter of class type (`enter_state`), or a temporary
        # (`Cmp{}`, `Cmp()`). None for anything else, so every other callee
        # and argument keeps its name-based resolution.
        if (class_qn := self._functor_class(node, module_qn, var_types)) is None:
            return None
        return self._resolver.try_resolve_method(class_qn, cs.CPP_OPERATOR_CALL_NAME)

    def _functor_class(
        self, node: Node, module_qn: str, var_types: dict[str, str] | None
    ) -> str | None:
        match node.type:
            case cs.TS_CPP_IDENTIFIER:
                return self._resolver.cpp.cpp_operand_class_qn(
                    safe_decode_text(node), var_types, module_qn
                )
            case cs.TS_CPP_COMPOUND_LITERAL_EXPRESSION:
                type_node = node.child_by_field_name(cs.FIELD_TYPE)
            case cs.TS_CPP_CALL_EXPRESSION:
                type_node = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            case _:
                return None
        if type_node is None or not (
            type_name := cpp_local_types.named_type(type_node)
        ):
            return None
        if not cpp_local_types.is_qualified_type(type_node) and (
            local_qn := self._local_type_under(
                self._local_anchors(node, module_qn), type_name
            )
        ):
            return local_qn
        class_qn = self._resolver.resolve_class_name(type_name, module_qn)
        # The same spelling calls a function (`make(x)`) as often as it
        # constructs a class (`Cmp(x)`); only a class is a functor.
        if (
            class_qn is None
            or self._resolver.function_registry.get(class_qn) != cs.NodeLabel.CLASS
        ):
            return None
        return class_qn

    @host_site_scoped
    def ingest_braced_return_instantiations(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
    ) -> None:
        # `return {args};` (nlohmann's exception factories) constructs the
        # caller's DECLARED return type through a bare initializer_list; no
        # call node exists, so the constructed class's ctor gets no edge and
        # reports dead even when its only factory is alive. Emit INSTANTIATES
        # to the class and CALLS to its ctors, like an explicit construction.
        # A lambda body is skipped: its returns are not the caller's.
        # Revive-only: nothing is emitted unless the return type resolves to a
        # registered first-party class.
        has_braced_return = False
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type == cs.TS_CPP_LAMBDA_EXPRESSION:
                continue
            if node.type == cs.TS_RETURN_STATEMENT and any(
                child.type == cs.TS_CPP_INITIALIZER_LIST
                for child in node.named_children
            ):
                has_braced_return = True
                break
            stack.extend(node.children)
        if not has_braced_return:
            return
        class_qn = self._resolver.cpp.cpp_braced_return_class(caller_qn, module_qn)
        if class_qn is None:
            return
        registry = self._resolver.function_registry
        ensure_rel = self._host.emit_rel
        # The return statement is the construction site (issue #1522).
        self._host.site_node = node
        for class_variant in registry.variants(class_qn):
            variant_type = registry.get(class_variant)
            if variant_type is not None and variant_type != cs.NodeLabel.CLASS:
                continue
            ensure_rel(
                caller_spec,
                cs.RelationshipType.INSTANTIATES,
                (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_variant),
            )
        self.emit_ctor_calls(caller_spec, class_qn)

    @host_site_scoped
    def ingest_member_init_ctor_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
    ) -> None:
        # A ctor's member initializer list runs base-class ctors
        # (`: buffer(g, 0)`) and delegated ctors (`: widget(0)`) with no
        # call_expression node, so a base ctor only ever reached through
        # derived member-init had zero incoming CALLS and reported dead
        # (fmt buffer.buffer). Each initializer whose head name resolves to a
        # registered class emits CALLS to that class's ctors; a member FIELD
        # initializer resolves to no class and emits nothing (a field named
        # exactly like a registered class still emits, the common
        # field-shadows-its-own-type case where the ctor does run). The list
        # is a DIRECT child of function_definition, so a nested lambda's or
        # local class's initializers never leak in.
        for init_list in caller_node.children:
            if init_list.type != cs.CppNodeType.FIELD_INITIALIZER_LIST:
                continue
            for initializer in init_list.named_children:
                if initializer.type != cs.CppNodeType.FIELD_INITIALIZER:
                    continue
                name = _member_init_head_name(initializer)
                class_qn = (
                    self._resolver.resolve_type_to_class_qn(name, module_qn)
                    if name
                    else None
                )
                if class_qn is not None:
                    self._host.site_node = initializer
                    self.emit_ctor_calls(caller_spec, class_qn)

    def ingest_implicit_base_lifecycle_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
    ) -> None:
        # A ctor whose member initializer list does not name a base still
        # runs that base's default ctor, and a dtor runs base dtors after
        # its own body; neither has an AST node (issue #892), so base-chain
        # lifecycles reached only implicitly reported dead (wonderous
        # Win32Window). The caller's identity comes from its qn (leaf ==
        # class simple name for a ctor, `~name` for a dtor, parent a
        # registered class), which covers out-of-class definitions whose
        # per-caller pass runs at module level. Registry guarded: only
        # bases resolved to registered classes emit, and a delegating ctor
        # (`: Derived(0)`) emits nothing because the delegated-to ctor owns
        # the base call.
        # Only a DEFINITION knows its member initializer list; the in-class
        # prototype registers under the same qn and also runs a per-caller
        # pass, and emitting from it would double every edge (and invent
        # base calls a bodied definition's initializer list excludes). A
        # `= default` member parses as function_definition, so the guard
        # keeps it: its implicit base call is guaranteed by the standard.
        if caller_node.type not in (
            cs.CppNodeType.FUNCTION_DEFINITION,
            cs.CppNodeType.INLINE_METHOD_DEFINITION,
        ):
            return
        registry = self._resolver.function_registry
        class_qn, sep, leaf = caller_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep or registry.get(class_qn) != cs.NodeLabel.CLASS:
            return
        simple = qn_markers.strip_dup_marker(class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
        is_ctor = leaf == simple
        is_dtor = leaf == f"{cs.CPP_DESTRUCTOR_PREFIX}{simple}"
        if not is_ctor and not is_dtor:
            return
        # The full INHERITS closure, not just direct bases: construction and
        # destruction run EVERY ancestor's ctor/dtor unconditionally, and an
        # intermediate whose definition lives outside the parsed source has
        # no bodied caller pass to carry the chain onward (PR #894).
        ancestors = self._registered_ancestors(class_qn)
        if not ancestors:
            return
        if is_dtor:
            for base_qn in ancestors:
                self._emit_lifecycle_targets(
                    caller_spec, self._resolver.cpp.cpp_destructor_targets(base_qn)
                )
            return
        named = self._member_init_class_qns(caller_node, module_qn)
        if class_qn in named:
            return
        for base_qn in ancestors:
            if base_qn in named:
                # The member-init pass owns this base's edge; its own deeper
                # ancestors stay in the walk via the closure.
                continue
            self._emit_lifecycle_targets(
                caller_spec, self._resolver.java.java_constructor_targets(base_qn)
            )

    def _registered_ancestors(self, class_qn: str) -> list[str]:
        # Registered classes in the INHERITS closure, nearest first.
        ancestors: list[str] = []
        seen = {class_qn}
        queue = list(self._resolver.facts.class_inheritance.get(class_qn, []))
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if self._resolver.function_registry.get(current) == cs.NodeLabel.CLASS:
                ancestors.append(current)
            queue.extend(self._resolver.facts.class_inheritance.get(current, []))
        return ancestors

    def _emit_lifecycle_targets(
        self,
        caller_spec: tuple[str, str, str],
        targets: set[tuple[str, str]],
    ) -> None:
        registry = self._resolver.function_registry
        for target_type, target_qn in sorted(targets):
            for variant in registry.variants(target_qn):
                self._host.emit_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (target_type, cs.KEY_QUALIFIED_NAME, variant),
                )

    def _member_init_class_qns(self, caller_node: Node, module_qn: str) -> set[str]:
        # Class qns the ctor's member initializer list names explicitly;
        # those base ctors are already emitted by the member-init pass.
        named: set[str] = set()
        for init_list in caller_node.children:
            if init_list.type != cs.CppNodeType.FIELD_INITIALIZER_LIST:
                continue
            for initializer in init_list.named_children:
                if initializer.type != cs.CppNodeType.FIELD_INITIALIZER:
                    continue
                name = _member_init_head_name(initializer)
                if name and (
                    resolved := self._resolver.resolve_type_to_class_qn(name, module_qn)
                ):
                    named.add(resolved)
        return named

    @host_site_scoped
    def ingest_declaration_ctor_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
    ) -> None:
        # A declaration-shaped stack construction has no call node, so its
        # ctor (and end-of-lifetime dtor) got no edge and reported dead
        # (issue #871). Two shapes: `Point origin(10, 10)` (an
        # init_declarator whose argument list is a direct child) and the
        # most-vexing-parse misparse `FlutterWindow window(project);` (a
        # "function declaration" whose arguments are bare in-scope locals).
        # Revive-only: nothing is emitted unless the declared type resolves
        # to a registered first-party class. Lambda bodies are walked
        # because their calls flat-attribute to the enclosing caller (the
        # general call pass does the same); local-class scopes are skipped,
        # their method bodies being genuinely separate callers. The walk
        # descends into a declaration's own children too: a lambda bound by
        # `auto g = [](...) { ... }` nests inside one.
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in cs.CPP_COMPOUND_TYPES:
                continue
            if node.type != cs.CppNodeType.DECLARATION:
                stack.extend(node.children)
                continue
            stack.extend(node.children)
            if not _declaration_is_construction(node):
                continue
            type_name = _declaration_type_name(node)
            class_qn = (
                self._resolver.resolve_type_to_class_qn(type_name, module_qn)
                if type_name
                else None
            )
            if class_qn is not None:
                self._host.site_node = node
                self._emit_construction_edges(caller_spec, class_qn)

    def _emit_construction_edges(
        self, caller_spec: tuple[str, str, str], class_qn: str
    ) -> None:
        registry = self._resolver.function_registry
        for class_variant in registry.variants(class_qn):
            variant_type = registry.get(class_variant)
            if variant_type is not None and variant_type != cs.NodeLabel.CLASS:
                continue
            self._host.emit_rel(
                caller_spec,
                cs.RelationshipType.INSTANTIATES,
                (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_variant),
            )
        self.emit_ctor_calls(caller_spec, class_qn)

    def emit_constructor_calls(
        self, ctx: CallScan, callee_qn: str, class_variants: list[str]
    ) -> bool:
        self.emit_ctor_calls(ctx.caller_spec, callee_qn)
        return True

    def emit_ctor_calls(self, caller_spec: tuple[str, str, str], class_qn: str) -> None:
        # Construction runs a ctor now and the dtor at end of lifetime;
        # neither has a call node, so both get the redirect. sorted(): the
        # target label is a hash-randomized StrEnum, so sort for determinism.
        registry = self._resolver.function_registry
        emitted_target_qns: set[tuple[str, str]] = set()
        for class_variant in registry.variants(class_qn):
            variant_type = registry.get(class_variant)
            if variant_type is not None and variant_type != cs.NodeLabel.CLASS:
                continue
            targets = self._resolver.java.java_constructor_targets(
                class_variant
            ) | self._resolver.cpp.cpp_destructor_targets(class_variant)
            for target_type, target_qn in sorted(targets):
                target_key = (target_type, target_qn)
                if target_key in emitted_target_qns:
                    continue
                emitted_target_qns.add(target_key)
                for variant in registry.variants(target_qn):
                    self._host.emit_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                    )


class CCallResolution(LanguageCalls):
    """Writes the references a C function stored as a value makes, bound the
    way C's scoping and linkage bind the name: the file's own definition, an
    object the file or a header it includes declares, or a definition with
    external linkage.

    Built and owned by `CallProcessor`. The resolver and the module paths are
    the pass's own, held by reference: the definition pass fills them as it
    registers each file."""

    __slots__ = (
        "_host",
        "_resolver",
        "_module_qn_to_file_path",
        "_file_scopes",
        "_include_closures",
    )

    def __init__(
        self,
        host: EdgeWriterHost,
        resolver: CallResolver,
        module_qn_to_file_path: dict[str, Path],
    ) -> None:
        self._host = host
        self._resolver = resolver
        self._module_qn_to_file_path = module_qn_to_file_path
        # What each C/C++ source declares at file scope, read when a bare
        # function name in C resolves outside its own file (issue #2529).
        self._file_scopes: dict[str, CFileScope] = {}
        # The repo headers each C/C++ file includes, directly or through
        # another header (#2593).
        self._include_closures: dict[str, frozenset[str]] = {}

    def reset_file_scopes(self) -> None:
        self._file_scopes = {}
        self._include_closures = {}

    def caller_name(self, func_node: Node, module_qn: str) -> str | None:
        # The name lives in a nested declarator, as in C++.
        func_name = cpp_utils.extract_function_name(func_node)
        # The definition pass names a macro-split C definition from its
        # `type` field (issue #2528); without the same name here its body is
        # skipped and every call in it is lost.
        if not func_name:
            func_name = cpp_utils.c_macro_split_function_name(func_node)
        return func_name or None

    def method_name(self, method_node: Node, module_qn: str) -> str | None:
        return cpp_utils.extract_function_name(method_node)

    def ingest_caller_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # The module's own values are scanned before the no-calls early
        # return, so only nested scopes here.
        if caller_type != cs.NodeLabel.MODULE:
            self.ingest_function_value_references(
                caller_node,
                caller_spec,
                module_qn,
                flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
            )

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        # A file-scope ops table or hook struct (`static hooks_t h = {
        # my_alloc, my_free };`) is wired when the program loads, often in
        # a file that makes no call at all.
        self.ingest_function_value_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
        )

    @host_site_scoped
    def ingest_function_value_references(
        self,
        scope_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        boundary_types: frozenset[str],
    ) -> None:
        # C's callbacks, vtables and ops tables store a function in a pointer
        # and call it through the pointer, a call the graph never sees (issue
        # #2529). The site that stores it references it instead, as Go's func
        # maps and C#/Dart's passed method groups do, so the REFERENCES edge
        # keeps it reachable for dead-code and the call graph.
        for ident in c_function_value_identifiers(scope_node, boundary_types):
            name = safe_decode_text(ident)
            if not name:
                continue
            targets, resolution = self._function_value_targets(name, module_qn)
            # A parameter, local or local enumeration constant of that name
            # hides the function.
            if (
                not targets
                or name in cpp_utils.cpp_enclosing_function_value_names(ident)
                or name in local_enumerator_names(ident)
            ):
                continue
            self._host.site_node = ident
            self._host.resolution = resolution
            for target_qn in targets:
                self._host.emit_rel(
                    caller_spec,
                    cs.RelationshipType.REFERENCES,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def _function_value_targets(
        self, name: str, module_qn: str
    ) -> tuple[list[str], str]:
        registry = self._resolver.function_registry
        # The file's own definition is what the name denotes, static or not.
        own_qn = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        if registry.get(own_qn) == cs.NodeLabel.FUNCTION:
            variants = registry.variants(own_qn)
            return variants, (
                cs.EdgeResolution.OVERLOAD
                if len(variants) > 1
                else cs.EdgeResolution.EXACT
            )
        # A variable or enumeration constant this file or a header it
        # includes declares is what the name denotes (#2593).
        if any(
            name in self._file_scope(visible).objects
            for visible in (module_qn, *self._include_closure(module_qn))
        ):
            return [], cs.EdgeResolution.EXACT
        # Otherwise it is an extern declaration the linker binds by name to a
        # definition with external linkage, which no file's `static` has.
        targets = [
            variant
            for qn in registry.find_ending_with(name)
            if registry.get(qn) == cs.NodeLabel.FUNCTION
            and self._links_externally(qn, name, module_qn)
            for variant in registry.variants(qn)
        ]
        return targets, cs.EdgeResolution.HEURISTIC

    def _links_externally(self, qn: str, name: str, referrer_qn: str) -> bool:
        module_qn = qn[: -len(name) - 1]
        file_path = self._module_qn_to_file_path.get(module_qn)
        # A namespace or class member, or a definition in another language.
        if file_path is None or not file_path.name.endswith(
            cs.C_EXTENSIONS + cs.CPP_EXTENSIONS
        ):
            return False
        internal = name in self._file_scope(module_qn).tu_local_functions
        if file_path.name.endswith(cs.C_CPP_SOURCE_EXTENSIONS):
            return not internal
        # A header's `static inline` is compiled into the files including it
        # and no other (#2593); a plain definition there links
        # externally like a source file's.
        return not internal or module_qn in self._include_closure(referrer_qn)

    def _include_closure(self, module_qn: str) -> frozenset[str]:
        if (cached := self._include_closures.get(module_qn)) is not None:
            return cached
        imports = self._resolver.import_processor
        import_mapping = imports.import_mapping
        # A header a later include displaced from its local name is still
        # included, so its `static inline`s compile into this file too.
        displaced = imports.cpp.displaced_include_targets()
        reached: set[str] = set()
        pending = [module_qn]
        while pending:
            current = pending.pop()
            for target in (
                *(import_mapping.get(current) or {}).values(),
                *displaced.get(current, ()),
            ):
                if target not in reached and target in self._module_qn_to_file_path:
                    reached.add(target)
                    pending.append(target)
        reached.discard(module_qn)
        closure = frozenset(reached)
        self._include_closures[module_qn] = closure
        return closure

    def _file_scope(self, module_qn: str) -> CFileScope:
        if (cached := self._file_scopes.get(module_qn)) is not None:
            return cached
        scope = CFileScope(frozenset(), frozenset())
        file_path = self._module_qn_to_file_path.get(module_qn)
        if file_path is not None and (
            entry := self._resolver.type_inference.ast_cache.load(file_path)
        ):
            scope = c_file_scope(entry[0])
        self._file_scopes[module_qn] = scope
        return scope
