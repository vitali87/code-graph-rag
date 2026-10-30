"""A function handed over as a value, and the edge that keeps it reachable.

A callable passed as an argument, stored in a table, returned or assigned is
invoked by whoever receives it, never by a call the graph can see. The
`CallbackEdges` the call pass builds resolves such a value in the scope that
hands it over, peels what wraps it (a TS cast, `.bind`, a container literal,
a ternary's result operands), finds an inline function by the qualified name
the definition pass registered it under, and writes the CALLS or REFERENCES
edge with the reference's own resolution. The reference walkers, the
callable-parameter flow and a language's call collaborator all write
through it, and it calls back through `CallbackHost` for the edge site and
the language's call strategy."""

from collections.abc import Callable, Iterable
from typing import NamedTuple, Protocol

from tree_sitter import Node

from .. import constants as cs
from ..node_text import safe_decode_text
from ..types_defs import FunctionRegistryTrieProtocol
from ..utils import qn_markers
from .call_host import CallStrategyHost, RecordedCallerHost
from .call_nodes import parse_call_arguments
from .call_resolver import Resolved
from .dart import calls as dart_calls
from .edge_site import EdgeSiteHost, host_site_scoped
from .js_ts import bindings as js_bindings
from .js_ts import calls as js_calls
from .language_calls import LanguageCalls

# Python sequence-literal node types used when scanning a scope for dispatch
# tables of function references.
PY_SEQUENCE_LITERAL_TYPES = frozenset({cs.TS_PY_LIST, cs.TS_PY_SET, cs.TS_PY_TUPLE})
# Python nodes that transparently wrap first-class values one level down:
# sequence literals, a bare multi-value return (expression_list), and
# parentheses. Dict pairs and ternaries need field-aware handling, matched
# separately in CallbackEdges.expand_py_first_class_values.
_PY_VALUE_WRAPPER_TYPES = PY_SEQUENCE_LITERAL_TYPES | frozenset(
    {cs.TS_PY_EXPRESSION_LIST, cs.TS_PARENTHESIZED_EXPRESSION}
)
# Node types of a call argument that may name a callable: a bare identifier
# (Python/Go/JS/TS), a Python attribute (self.method), a Go selector (x.Method),
# or a JS/TS member expression (obj.method).
FLOW_ARG_REF_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_ATTRIBUTE,
        cs.TS_SELECTOR_EXPRESSION,
        cs.TS_MEMBER_EXPRESSION,
    }
)
# Node types of a value that hands a callable over BY NAME, in every language
# the argument-reference path serves: the flow-arg names above, a C# member
# access or generic method group, a Dart tear-off's `.name` selector (its
# argument unwraps to the last selector), a C++ qualified, template or field
# name, and a JS shorthand property. (C++ address-of is checked on its own in
# `_names_a_callable`.) An operator expression is absent on purpose: it
# computes a value, and its text resolved as a name against a builtin receiver
# gave `builtin.Array.prototype.length - start` (issue #2438).
_CALLBACK_REF_TYPES = FLOW_ARG_REF_TYPES | frozenset(
    {
        cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION,
        cs.TS_CSHARP_GENERIC_NAME,
        cs.TS_DART_SELECTOR,
        cs.TS_CPP_QUALIFIED_IDENTIFIER,
        cs.TS_CPP_TEMPLATE_FUNCTION,
        cs.TS_CPP_FIELD_EXPRESSION,
        cs.TS_SHORTHAND_PROPERTY_IDENTIFIER,
    }
)


def scope_qn_candidates(scope_qn: str) -> list[str]:
    """The scope itself plus its duplicate-variant-stripped form."""
    # The scope itself plus its duplicate-variant-stripped form (`useStore@27`
    # -> `useStore`): the def pass registers nested/anon members under the
    # NATURAL qn while the caller may carry the variant suffix. Registry-guarded,
    # so a scope without a twin adds nothing.
    # Compare against the STRIPPED form rather than testing for the marker
    # character: a C# verbatim identifier (`@event`) contains it without
    # carrying a marker, and would otherwise add a duplicate candidate that
    # is merely the scope again (issue #2017).
    natural = qn_markers.natural_qn(scope_qn)
    if natural == scope_qn:
        return [scope_qn]
    return [scope_qn, natural]


def scoped_candidates(scope_qn: str, names: Iterable[str]) -> set[str]:
    """Each name as a member of the scope and of its variant-stripped form."""
    # Every reference path that names a member of the caller's scope must try
    # the variant-stripped scope too; building the candidates in one place
    # keeps a new path from repeating the object-literal gap, where only the
    # inline-argument path tried it.
    return {
        f"{scope}{cs.SEPARATOR_DOT}{name}"
        for scope in scope_qn_candidates(scope_qn)
        for name in names
    }


def _names_a_callable(node: Node) -> bool:
    # C/C++ also hands a function over by its address (`run(&Cls::m)`), the
    # one operator expression that names rather than computes; a dereference
    # (`*p`) or any other operator does not.
    if node.type == cs.TS_CPP_POINTER_EXPRESSION:
        operator = node.child_by_field_name(cs.FIELD_OPERATOR)
        return operator is not None and safe_decode_text(operator) == cs.CPP_ADDRESS_OF
    return node.type in _CALLBACK_REF_TYPES


def _first_class_value_children(
    node: Node, is_dart: bool, short_circuits: bool = False
) -> list[Node] | None:
    # The wrapped value nodes a container/branch node hands onward, or None
    # for a leaf that IS the first-class value.
    if node.type in _PY_VALUE_WRAPPER_TYPES:
        return list(node.named_children)
    if is_dart and node.type in dart_calls.VALUE_WRAPPER_TYPES:
        return dart_calls.collection_values(node)
    if node.type == cs.TS_PY_DICTIONARY:
        return _py_dict_values(node)
    if node.type in (cs.TS_PY_CONDITIONAL_EXPRESSION, cs.TS_JS_TERNARY_EXPRESSION):
        return conditional_result_operands(node, is_dart)
    return _operand_value_children(node, is_dart, short_circuits)


def _operand_value_children(
    node: Node, is_dart: bool, short_circuits: bool
) -> list[Node] | None:
    # The operands a cast or an operator node hands onward as its value, or
    # None for a node that hands none.
    # A TS cast (`(() => {}) as any`, `fn satisfies T`, `cb!`) is transparent for
    # first-class-value resolution: the wrapped value is the first named child.
    if node.type in cs.TS_CAST_WRAPPER_TYPES and node.named_children:
        return [node.named_children[0]]
    # A JS/TS short-circuit operator (`fn ?? (() => {})`, `a || cb`) binds one of
    # its OPERANDS as the value, exactly like Python's boolean_operator; JS/TS
    # spells it `binary_expression`. Gated to the languages whose operator does
    # so: Go and C also use `binary_expression` with `||`/`&&`, where a bare-name
    # operand resolves to a boolean, not a callable, and expanding it would
    # fabricate a phantom edge.
    if short_circuits and _is_short_circuit(node):
        return list(node.named_children)
    if is_dart and (swallowed := _dart_swallowed_conditional(node)) is not None:
        return [swallowed]
    if node.type == cs.TS_PY_BOOLEAN_OPERATOR:
        return [
            operand
            for operand in (
                node.child_by_field_name(cs.TS_FIELD_LEFT),
                node.child_by_field_name(cs.TS_FIELD_RIGHT),
            )
            if operand is not None
        ]
    return None


def _is_short_circuit(node: Node) -> bool:
    return (
        node.type == cs.TS_BINARY_EXPRESSION
        and (operator := node.child_by_field_name(cs.FIELD_OPERATOR)) is not None
        and safe_decode_text(operator) in cs.JS_SHORT_CIRCUIT_OPERATORS
    )


def _dart_swallowed_conditional(node: Node) -> Node | None:
    if (
        node.type.endswith(cs.DART_EXPRESSION_NODE_SUFFIX)
        # A scope-opening node (function_expression) IS the first-class
        # value; only flat operator nodes hand a swallowed ternary onward.
        and node.type not in cs.DART_NESTED_SCOPE_NODE_TYPES
        and (kids := node.named_children)
        and kids[-1].type == cs.TS_PY_CONDITIONAL_EXPRESSION
    ):
        # tree-sitter-dart parses low-precedence operators OVER the ternary
        # (`a + b > 0 ? f : null` -> additive_expression(a, +,
        # conditional_expression(...))), swallowing the conditional as the
        # LAST child of the operator node; its result operands are still the
        # handed-over values (issue #873).
        return kids[-1]
    return None


def _py_dict_values(node: Node) -> list[Node]:
    return [
        pair_value
        for pair in node.named_children
        if pair.type == cs.TS_PY_PAIR
        and (pair_value := pair.child_by_field_name(cs.FIELD_VALUE)) is not None
    ]


def conditional_result_operands(node: Node, is_dart: bool) -> list[Node]:
    """The operands a conditional evaluates to, never its condition."""
    # tree-sitter-python exposes NO field names on conditional_expression
    # (child_by_field_name returns None for every operand), so the result
    # operands are positional: [body, condition, alternative]. A shape that
    # is not exactly three named operands falls back to all of them,
    # over-referencing rather than dropping a branch.
    operands = list(node.named_children)
    if len(operands) != 3:
        return operands
    # Dart and JS/TS (`ternary_expression`) order [condition, consequence,
    # alternative]; Python (`conditional_expression`) orders [body, condition,
    # alternative]. Pick the two result operands, never the truthiness-tested one.
    condition_first = is_dart or node.type == cs.TS_JS_TERNARY_EXPRESSION
    return [operands[1], operands[2]] if condition_first else [operands[0], operands[2]]


def verdict_of(resolved: tuple[str, str] | None) -> str:
    """How surely a callee binds: the resolver's own verdict, and exact for an
    answer a frontend or a language engine gave, which never guesses."""
    return (
        resolved.resolution
        if isinstance(resolved, Resolved)
        else cs.EdgeResolution.EXACT
    )


class RefEmitScope(NamedTuple):
    # The caller-side context a reference walk hands every edge it emits
    # through CallbackEdges.emit_callback_edge.
    caller_spec: tuple[str, str, str]
    module_qn: str
    local_var_types: dict[str, str] | None
    class_context: str | None
    caller_qn: str | None
    resolve_func: Callable[..., tuple[str, str] | None]
    ensure_rel: Callable[..., None]


class CallbackHost(EdgeSiteHost, CallStrategyHost, RecordedCallerHost, Protocol):
    """What the callback edges call on the call pass that builds them: the
    call strategy of the language a value is written in, and where the
    definition pass recorded a function node. Beside the edge site, whose
    resolution a fan-out of one reference marks while it writes."""


class CallbackEdges:
    """Resolves a function handed over as a value and writes its edge.

    Built and owned by `CallProcessor`, whose registry the targets are
    checked against."""

    __slots__ = ("_host", "_registry")

    def __init__(
        self, host: CallbackHost, function_registry: FunctionRegistryTrieProtocol
    ) -> None:
        self._host = host
        self._registry = function_registry

    def expand_py_first_class_values(
        self,
        value: Node,
        language: cs.SupportedLanguage | None = None,
        opened: set[Node] | None = None,
    ) -> list[Node]:
        """The first-class values `value` hands over, its wrappers peeled."""
        # Peel Python container literals and result-position conditional
        # operands so a function stored in a tuple/list/set, a dict VALUE,
        # a bare return-tuple (expression_list), or a ternary/boolean-default
        # branch is treated like a bare first-class value; nesting expands
        # recursively. A ternary's condition is only truthiness-tested, never
        # bound, so it stays excluded. Any other node comes back unchanged, so
        # non-Python shapes are unaffected. `opened`, when given, collects
        # every container peeled, so a walk that meets one again later knows
        # its values are already out.
        is_dart = language == cs.SupportedLanguage.DART
        short_circuits = self._host.calls_for(language).short_circuit_values
        out: list[Node] = []
        stack = [value]
        while stack:
            node = stack.pop()
            children = _first_class_value_children(node, is_dart, short_circuits)
            if children is None:
                out.append(node)
            else:
                if opened is not None:
                    opened.add(node)
                stack.extend(reversed(children))
        return out

    def emit_scope_ref(self, scope: RefEmitScope, operand: Node) -> None:
        """A REFERENCES edge from a walk's caller to the callable `operand`
        names, resolved in the walk's own scope."""
        self.emit_callback_edge(
            scope.caller_spec,
            operand,
            scope.module_qn,
            scope.local_var_types,
            scope.class_context,
            scope.resolve_func,
            scope.ensure_rel,
            scope.caller_qn,
            cs.RelationshipType.REFERENCES,
        )

    @host_site_scoped
    def emit_callback_edge(
        self,
        source_spec: tuple[str, str, str],
        arg_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.CALLS,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        """Write the edge a value handed over by name or inline keeps alive."""
        # A TS cast (`handler as any`, `fn satisfies T`, `cb!`) is transparent
        # for reference resolution, and `fn.bind(ctx)` / `.call` / `.apply` in
        # argument position (addEventListener("click", handler.bind(this)),
        # django admin's inlines.js) hands off `fn` while the call itself
        # resolves to the Function.prototype builtin; peel both to a fixpoint.
        self._host._site_node = arg_node
        arg_node = js_calls.peel_bound_callable(arg_node)
        # An arrow/function-expression passed DIRECTLY as a call argument
        # (useCallback(() => {}), setTimeout(() => {}), arr.map(x => ...)) is
        # registered anonymously in the enclosing scope but named after no
        # identifier, so resolve_func cannot find it. The call consumes it, so
        # reference it by position the same way inline object-literal values are.
        if arg_node.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
            self.emit_inline_arg_function_ref(
                arg_node, source_spec, ensure_rel, caller_qn, rel_type, module_qn
            )
            return
        language_calls = self._host.calls_for(language)
        if language_calls.emit_functor_callback(
            arg_node, source_spec, module_qn, local_var_types, ensure_rel
        ):
            return
        # Only a name can hand a callable over. The whole source text of any
        # other argument (`items.length - start`, `-x`, `xs[0]`) was resolved
        # as if it were one (issue #2438).
        if not _names_a_callable(arg_node):
            return
        if not (arg_text := safe_decode_text(arg_node)):
            return
        if (
            arg_text := language_calls.callback_name(
                arg_node,
                arg_text,
                source_spec,
                module_qn,
                local_var_types,
                caller_qn,
                ensure_rel,
                rel_type,
            )
        ) is None:
            return
        if not (
            resolved := resolve_func(
                arg_text, module_qn, local_var_types, class_context, caller_qn
            )
        ):
            return
        res_type, res_qn = resolved
        resolution = verdict_of(resolved)
        if res_type == cs.NodeLabel.CLASS:
            constructor = self._class_callback_target(
                source_spec,
                res_qn,
                rel_type,
                ensure_rel,
                module_qn,
                language,
                resolution,
            )
            if constructor is None:
                return
            res_type, res_qn = constructor
        if not self._is_callback_target(res_type, res_qn, language_calls):
            return
        self.emit_callback_targets(
            source_spec,
            res_type,
            res_qn,
            rel_type,
            ensure_rel,
            module_qn,
            language,
            resolution,
        )

    def _is_callback_target(
        self, res_type: str, res_qn: str, language_calls: LanguageCalls
    ) -> bool:
        # Only callables are meaningful callback/reference targets: a value can
        # resolve to an Interface/Type node (`selector = identity as Selector`
        # resolves the cast's TYPE name in some paths), and emitting that
        # produces schema-invalid edges.
        if res_type not in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            return False
        # And only a callable the graph has a node for. A member read on a
        # builtin-typed receiver (`error.customDelay` on an Error,
        # `items.length` on an Array) resolves to a synthetic `builtin.*` qn
        # that is never created, a property as often as a method, so the
        # edge could only be dropped by the database and counted as a failed
        # write on every index (issue #2438; #652 for the call form).
        if res_qn not in self._registry:
            return False
        # A Dart getter in argument position is a VALUE READ, not a tear-off:
        # the getter-read pass owns those edges (with shadow handling), so
        # emitting here would fabricate liveness for a local that hides the
        # getter (issue #869).
        return not (
            language_calls.property_args_are_reads
            and self._registry.is_property(res_qn)
        )

    def _class_callback_target(
        self,
        source_spec: tuple[str, str, str],
        class_qn: str,
        rel_type: cs.RelationshipType,
        ensure_rel: Callable[..., None],
        module_qn: str,
        language: cs.SupportedLanguage | None,
        resolution: str,
    ) -> tuple[str, str] | None:
        # A class passed as a callback is called through its constructor.
        # None when the edges were already fanned out here, or when the
        # class has no `__init__` node to reference.
        registry = self._registry
        if any(
            registry.get(variant) in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
            for variant in registry.variants(class_qn)
        ):
            # A same-named def is passed on with the class (issue
            # #2621); the fan-out gives each variant its own target.
            self.emit_callback_targets(
                source_spec,
                cs.NodeLabel.CLASS,
                class_qn,
                rel_type,
                ensure_rel,
                module_qn,
                language,
                resolution,
            )
            return None
        init_qn = f"{class_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
        if init_qn not in registry:
            return None
        return cs.NodeLabel.METHOD, init_qn

    def emit_callback_targets(
        self,
        source_spec: tuple[str, str, str],
        res_type: str,
        res_qn: str,
        rel_type: cs.RelationshipType,
        ensure_rel: Callable[..., None],
        module_qn: str,
        language: cs.SupportedLanguage | None,
        resolution: str,
    ) -> None:
        """Write one edge per registered variant of a resolved callback."""
        registry = self._registry
        host = self._host
        # The callback's OWN verdict, not the enclosing call's: `resolution`
        # is how surely this reference resolved; the host's `_resolution` still
        # describes the call whose argument it is. Emitting through the stale value labelled a callback
        # found only by trie fallback as exact, so `dead-code --min-resolution
        # exact` kept a target it should have dropped (#1543). Scoped:
        # the enclosing call's label is restored on the way out, so the edge
        # it emits afterwards reads its own verdict back (issue #1526).
        # And, like a primary call, a callback that fans out to same-named
        # candidates is an `overload`: one reference, several targets, none
        # chosen (#1543). The resolver's single-target verdict would
        # otherwise let an exact floor keep every one of them.
        prev_resolution = host._resolution
        language_calls = host.calls_for(language)
        targets = registry.variants(res_qn)
        host._resolution = (
            cs.EdgeResolution.OVERLOAD if len(targets) > 1 else resolution
        )
        try:
            for variant in targets:
                # A variant can be another kind than the resolved callback:
                # a Python class beside a same-named def. Written under the
                # callback's label it named no node, so the database dropped
                # it (issue #2621). A class is passed on through its __init__,
                # as `emit_callback_edge` passes a lone class.
                match registry.get(variant):
                    case cs.NodeLabel.FUNCTION | cs.NodeLabel.METHOD as kind:
                        target_type, target_qn = kind, variant
                    case None:
                        target_type, target_qn = res_type, variant
                    case cs.NodeLabel.CLASS if (
                        init_qn := f"{variant}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
                    ) in registry:
                        target_type, target_qn = cs.NodeLabel.METHOD, init_qn
                    case _:
                        continue
                ensure_rel(
                    source_spec,
                    rel_type,
                    (target_type, cs.KEY_QUALIFIED_NAME, target_qn),
                )
                language_calls.record_cross_module_use(module_qn, target_qn)
        finally:
            host._resolution = prev_resolution

    @host_site_scoped
    def emit_inline_arg_function_ref(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.REFERENCES,
        module_qn: str | None = None,
    ) -> None:
        """Reference an inline function value by the node the definition
        pass registered for it."""
        # The node's OWN source span records the qn the definition pass minted,
        # so resolve by span first when the module is known: it is authoritative
        # across the two ways caller_qn can mislead a name/position guess. An
        # object-literal method nested in a bound arrow is named FLAT (`mod.
        # update`, dropping the nameless `make`) while the function it contains
        # nests a level deeper (`mod.make.update.anonymous_R_C`), so no
        # `{caller_qn}.anonymous_R_C` candidate matches (issue #985); and a NESTED
        # named function-expression (`mod.make.update.handler`) whose name
        # collides with a module-level function would otherwise match the
        # module-flat name candidate and reference the WRONG top-level node. The
        # candidate fallback is kept for an incremental run where an unchanged
        # file's span was not re-recorded this pass.
        self._host._site_node = arg_node
        if (
            module_qn is not None
            and (loc := self._host.recorded_caller(arg_node, module_qn)) is not None
        ):
            ensure_rel(
                source_spec,
                rel_type,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, loc.qualified_name),
            )
            return
        self._emit_inline_ref_candidates(
            arg_node, source_spec, ensure_rel, caller_qn, rel_type, module_qn
        )

    def _emit_inline_ref_candidates(
        self,
        arg_node: Node,
        source_spec: tuple[str, str, str],
        ensure_rel: Callable[..., None],
        caller_qn: str | None,
        rel_type: cs.RelationshipType,
        module_qn: str | None,
    ) -> None:
        # An inline arrow/function-expression call argument is registered by the
        # definition pass as {enclosing_scope}.anonymous_<row>_<col> from its own
        # start position. The anonymous node lives in the CALLER's scope, so build
        # the candidate from caller_qn (source_spec[2] is the callee for the
        # callable-param path). Registry guard skips unregistered names.
        scope_qn = caller_qn or source_spec[2]
        suffixes = [
            f"{cs.SEPARATOR_DOT}{cs.PREFIX_ANONYMOUS}"
            f"{arg_node.start_point[0]}_{arg_node.start_point[1]}"
        ]
        # A NAMED function expression argument (`this.on('mount', function
        # onmount(parent) {...})`, express) registers by its own NAME; the
        # position form never matches it, so try the name too, both under the
        # caller scope and module-flat (where the def pass puts it).
        name_node = arg_node.child_by_field_name(cs.FIELD_NAME)
        named = safe_decode_text(name_node) if name_node is not None else None
        if named:
            suffixes.append(f"{cs.SEPARATOR_DOT}{named}")
        # A duplicate-variant caller (a TS overload implementation registers as
        # `useStore@27`) owns anons the def pass registers under the NATURAL qn
        # (`useStore.anonymous_R_C`); try the variant-stripped scope too.
        scopes = scope_qn_candidates(scope_qn)
        if named and module_qn is not None and module_qn not in scopes:
            scopes = [*scopes, module_qn]
        self.reference_registered(
            source_spec,
            [f"{scope}{suffix}" for scope in scopes for suffix in suffixes],
            ensure_rel,
            rel_type,
        )

    def reference_registered(
        self,
        source_spec: tuple[str, str, str],
        candidates: Iterable[str],
        ensure_rel: Callable[..., None],
        rel_type: cs.RelationshipType,
    ) -> None:
        """Reference every registered variant of the candidate qns."""
        # Only a name the definition pass registered gets an edge, each of its
        # duplicate variants included, so an over-collected candidate costs a
        # lookup and never a dangling relationship.
        registry = self._registry
        for candidate in candidates:
            for target_qn in registry.variants(candidate):
                if registry.get(target_qn) is None:
                    continue
                ensure_rel(
                    source_spec,
                    rel_type,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def ingest_argument_function_references(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.CALLS,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        """Write the edge each function a call passes as an argument keeps
        alive."""
        # A function/method passed as an argument is a first-class value the
        # callee may invoke (external framework) or store for later dynamic
        # dispatch (first-party plumbing); either way the passing scope holds a
        # live reference, so emit an edge to keep the callback reachable.
        # External/builtin callees keep the CALLS edge; first-party callees
        # record the pass as REFERENCES (the precise invocation edge, if any,
        # comes from callable-param flow).
        positional, keyword = parse_call_arguments(call_node)
        for arg_node in (*positional, *keyword.values()):
            # A container-literal or conditional argument
            # (`validators=[_simple_domain_name_validator]`, django's Site;
            # `... if single else local_setter_noop`, its SQLCompiler) hides
            # the passed functions one level down; each expanded value is a
            # first-class reference exactly like a bare-name argument.
            for value_node in self.expand_py_first_class_values(arg_node, language):
                self.emit_callback_edge(
                    caller_spec,
                    value_node,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                    rel_type,
                    language,
                )
