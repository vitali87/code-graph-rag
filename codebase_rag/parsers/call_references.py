"""The reference walkers: the edges a scope makes to the functions it holds
as values, with no call node to carry them.

A function a scope assigns to a name, returns, defaults a parameter to or
renders as a JSX component is invoked later by whoever receives it (one its
literals hold is `call_tables`'). Each walker scans one scope's own syntax,
stopping at the nested scopes that get a pass of their own (but walking on
through an anonymous arrow no caller pass covers), and writes the edge
through the `CallbackEdges` it shares with the call pass. `CallProcessor`
and the languages' call collaborators run the walkers for the languages
whose syntax holds such values; the walkers call back through
`ReferenceHost` for the edge site, the edge writer and a language's call
strategy."""

from collections.abc import Callable, Mapping

from tree_sitter import Node

from .. import constants as cs
from ..node_text import safe_decode_text
from ..types_defs import LanguageQueries
from .call_callbacks import CallbackEdges, RefEmitScope, conditional_result_operands
from .call_resolver import CallResolver
from .call_tables import ReferenceHost, TableReferences, UnownedScopes
from .callable_params import js_ts_parameter_names
from .edge_site import host_site_scoped
from .js_ts import bindings as js_bindings
from .js_ts import calls as js_calls
from .scope_boundaries import flow_scope_boundaries

# Assignment node type -> RHS field, per language family: Python `assignment`
# and JS/TS `assignment_expression` (client.post = fn) carry the RHS in
# `right`; a JS/TS `variable_declarator` (const cb = handler) carries it in
# `value`. Go binds a func value the same way; its RHS sits behind an
# expression_list, unwrapped in the walker.
_ASSIGNMENT_RHS_FIELDS = {
    cs.TS_PY_ASSIGNMENT: cs.TS_FIELD_RIGHT,
    cs.TS_ASSIGNMENT_EXPRESSION: cs.TS_FIELD_RIGHT,
    # A JS/TS logical-assign default (`obj.when ??= (p) => {...}`, `x ||= fn`)
    # stores the RHS function for later dynamic dispatch, exactly like a plain
    # assignment, so its RHS is scanned the same way (same `right` field). Only a
    # callable RHS (an inline arrow or a name resolving to a function) yields an
    # edge; a data RHS (`count += 1`) resolves to nothing and adds none.
    cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION: cs.TS_FIELD_RIGHT,
    # A class field bound to a NAMED first-class function value (`log = noop`)
    # references it like a `const` declarator. (An inline-arrow field value is a
    # method definition, referenced elsewhere, not a name to resolve here.)
    cs.TS_PUBLIC_FIELD_DEFINITION: cs.FIELD_VALUE,
    cs.TS_VARIABLE_DECLARATOR: cs.FIELD_VALUE,
    cs.TS_GO_VAR_SPEC: cs.FIELD_VALUE,
    cs.TS_GO_SHORT_VAR_DECLARATION: cs.TS_FIELD_RIGHT,
    cs.TS_GO_ASSIGNMENT_STATEMENT: cs.TS_FIELD_RIGHT,
}
# RHS node types that name a callable value: a bare identifier, a Python
# attribute (mod.fn), a JS/TS member expression (handlers.run), or a Go
# selector (pkg.Fn).
_ASSIGNMENT_RHS_REF_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_ATTRIBUTE,
        cs.TS_MEMBER_EXPRESSION,
        cs.TS_GO_SELECTOR_EXPRESSION,
    }
)
# JSX nodes that carry a component name (self-closing and opening; a
# closing element repeats the name, so a paired element emits once).
_JSX_NAMED_ELEMENT_TYPES = frozenset(
    {cs.TS_JSX_SELF_CLOSING_ELEMENT, cs.TS_JSX_OPENING_ELEMENT}
)
# What a JSX tag can render: a function or class component (an arrow bound to
# a const registers as a Function). An Interface/Type/Enum of the same name is
# never renderable, so a tag resolving to one is a name collision (issue #2535).
_JSX_COMPONENT_TARGET_TYPES = frozenset(
    {cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD, cs.NodeLabel.CLASS}
)


class ReferenceWalkers:
    """The walks that reference the functions a scope holds as values.

    Built and owned by `CallProcessor`; a reference resolves through the
    pass's resolver, in the scope of the walk that finds it."""

    __slots__ = ("_host", "_callbacks", "_tables", "_resolver", "_scopes")

    def __init__(
        self,
        host: ReferenceHost,
        callbacks: CallbackEdges,
        tables: TableReferences,
        resolver: CallResolver,
        scopes: UnownedScopes,
    ) -> None:
        self._host = host
        self._callbacks = callbacks
        self._tables = tables
        self._resolver = resolver
        self._scopes = scopes

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_field_assignments: bool = False,
    ) -> None:
        """Reference the functions a module's dispatch tables and first-class
        assignments hold, outside any function scope."""
        # A Python class body (import time) or a JS/TS class-field
        # initializer (construction time) can store a dispatch table
        # as a class ATTRIBUTE (django's `data_types = {"CharField":
        # _get_varchar_...}`; a JS `opts = { retryStrategy }`), which
        # is construction-load wiring like a module-level table; the
        # scan descends through classes but still stops at function
        # scopes. ponytail: decorated classes stay boundaries
        # (decorated_definition also wraps functions).
        collection_boundaries = flow_scope_boundaries(
            queries[language][cs.QUERY_CONFIG]
        ) - frozenset(queries[language][cs.QUERY_CONFIG].class_node_types)
        self._tables.ingest_collection_function_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            None,
            None,
            collection_boundaries,
            language,
        )
        # A module-scope first-class assignment (registry_handler =
        # handle_event, module.exports.run = run) references its target
        # even in a file with no calls, so scan before the no-calls
        # early return.
        assignment_boundaries = flow_scope_boundaries(
            queries[language][cs.QUERY_CONFIG]
        )
        if class_field_assignments:
            # A class-field initializer (`log = noop`) runs at construction,
            # so descend into class bodies to reference the function it
            # binds; the scan still stops at method/function scopes, so a
            # local assignment inside a method stays theirs.
            assignment_boundaries = assignment_boundaries - frozenset(
                queries[language][cs.QUERY_CONFIG].class_node_types
            )
        self.ingest_assignment_function_references(
            root_node,
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            module_qn,
            None,
            None,
            assignment_boundaries,
            language=language,
        )

    @host_site_scoped
    def ingest_assignment_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        """Reference the functions a scope's assignments bind to a name."""
        # `x = some_function` binds a first-class function value to a name; the
        # alias is then stored, passed onward, or returned for dynamic dispatch
        # (http_callback = llm_http_task_closure_with_context), so the assignment
        # itself references the function and must keep it reachable. Only a plain
        # name/attribute RHS counts (calls resolve as calls); the walk stops at
        # nested scope boundaries, which own their own pass.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._host._emit_rel
        scope = RefEmitScope(
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            caller_qn,
            resolve_func,
            ensure_rel,
        )
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            # Continue THROUGH an unowned anonymous arrow (zustand's curried
            # middleware body `(config) => (set, get, api) => { api.setState =
            # ... }`): it gets no caller pass, so its assignments would else be
            # scanned by nobody and the stored functions report dead. Named
            # nested scopes still own their own pass.
            if node.type in boundary_types and not self._scopes.is_unowned_scope(
                node, module_qn
            ):
                continue
            if rhs_field := _ASSIGNMENT_RHS_FIELDS.get(node.type):
                self._ingest_assignment_rhs(node, rhs_field, scope, language)
            stack.extend(node.children)

    def _ingest_assignment_rhs(
        self,
        node: Node,
        rhs_field: str,
        scope: RefEmitScope,
        language: cs.SupportedLanguage | None,
    ) -> None:
        right = node.child_by_field_name(rhs_field)
        # Go wraps every assignment RHS in an expression_list
        # (`var f = fn`, `a, b = g, h`); scan each value so the bare
        # func name(s) underneath are referenced. Other languages
        # carry a lone RHS node.
        values = (
            list(right.named_children)
            if right is not None and right.type == cs.TS_GO_EXPRESSION_LIST
            else [right]
        )
        for value in values:
            if value is None:
                continue
            self._ingest_assignment_value(node, value, scope, language)

    def _ingest_assignment_value(
        self,
        node: Node,
        value: Node,
        scope: RefEmitScope,
        language: cs.SupportedLanguage | None,
    ) -> None:
        # `export const persist = persistImpl as unknown as Persist`
        # wraps the aliased impl in TS casts, and devtools' shape
        # interleaves parens (`api.setState = ((s, r) => {...}) as
        # SetState`); peel both so the bare name/arrow underneath is
        # what we reference.
        # `const bound = handler.bind(null)` stores the bound
        # handler; .bind/.call/.apply are transparent for
        # reference resolution like a cast.
        value = js_calls.peel_bound_callable(value, peel_parens=True)
        # A bare-name RHS names a callable; an inline arrow/function-expr
        # RHS (`OpenAPI.TOKEN = async () => {}`) stores an anonymous
        # function on the target for later invocation, and
        # emit_callback_edge references it by position. A named
        # arrow-const RHS is registered by its name, so the by-position
        # lookup finds nothing.
        # A LOGICAL DEFAULT RHS (`done = done || function (err,
        # str) {...}`, express's render; `options.handler =
        # options.handler || defaultHandler`, fastify, issue
        # #990) evaluates TO one of its operands, so a NAMED
        # function operand flows as the assigned value exactly
        # like an inline one; chains (`a || b || defaultFmt`)
        # nest further logical binaries on the left. Only the
        # short-circuit operators qualify: a comparison or
        # arithmetic operand (`x === fn`, `n + fn`) never IS the
        # assigned value, so named operands there stay silent
        # while inline functions keep the historical reference.
        if value.type == cs.TS_BINARY_EXPRESSION:
            self._emit_binary_rhs_refs(value, scope, language)
            return
        # A Python TERNARY / BOOLEAN-DEFAULT RHS (`get_response =
        # self._async if is_async else self._sync`, django's
        # BaseHandler; `f = handler or fallback`) binds one of its
        # RESULT operands; each result operand naming a callable is a
        # possible referent. A ternary's condition is only
        # truthiness-tested, never bound, so it is excluded; both
        # boolean operands are possible results.
        if value.type in (
            cs.TS_PY_CONDITIONAL_EXPRESSION,
            cs.TS_JS_TERNARY_EXPRESSION,
            cs.TS_PY_BOOLEAN_OPERATOR,
        ):
            self._emit_conditional_rhs_refs(value, scope)
            return
        if (
            value.type in _ASSIGNMENT_RHS_REF_TYPES
            or value.type in js_bindings.INLINE_FUNC_VALUE_TYPES
        ):
            self._callbacks.emit_scope_ref(scope, value)
            # A member-assigned inline function (`api.setState =
            # (s, r) => {...}`) is ALSO registered by the def pass
            # under the PROPERTY name (scope.setState), which the
            # by-position anonymous candidate above never matches;
            # reference that registration too or it reports dead.
            if value.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
                self._emit_assigned_name_ref(
                    node, scope.caller_spec, scope.ensure_rel, scope.caller_qn
                )

    def _emit_binary_rhs_refs(
        self,
        value: Node,
        scope: RefEmitScope,
        language: cs.SupportedLanguage | None,
    ) -> None:
        # Named-operand emission is JS/TS-only: Go reaches
        # this walk too, and its boolean `||`/`&&` operands
        # would fabricate phantom edges (a bool named like a
        # method falsely revives it).
        short_circuits = self._host._calls_for(language).short_circuit_values
        is_logical = short_circuits and js_calls.is_logical_binary(value)
        # Position matters for `&&` only: a function value
        # is always truthy, so the LEFT operand of `&&` can
        # never be the expression's result; `||`/`??` flow
        # either operand.
        operands = self._logical_rhs_operands(value, is_logical)
        for operand, value_position in operands:
            if operand.type in js_bindings.INLINE_FUNC_VALUE_TYPES or (
                is_logical
                and value_position
                and operand.type in _ASSIGNMENT_RHS_REF_TYPES
            ):
                self._callbacks.emit_scope_ref(scope, operand)

    def _logical_rhs_operands(
        self, value: Node, is_logical: bool
    ) -> list[tuple[Node, bool]]:
        operands: list[tuple[Node, bool]] = []
        pending: list[tuple[Node, bool]] = [(value, True)]
        while pending:
            raw, value_position = pending.pop()
            operand = js_calls.unwrap_ts_value(raw)
            if (
                is_logical
                and operand.type == cs.TS_BINARY_EXPRESSION
                and js_calls.is_logical_binary(operand)
            ):
                is_and = (
                    safe_decode_text(operand.child_by_field_name(cs.FIELD_OPERATOR))
                    == cs.JS_OPERATOR_LOGICAL_AND
                )
                left = operand.child_by_field_name(cs.FIELD_LEFT)
                right = operand.child_by_field_name(cs.TS_FIELD_RIGHT)
                if left is not None:
                    pending.append((left, value_position and not is_and))
                if right is not None:
                    pending.append((right, value_position))
                continue
            operands.append((operand, value_position))
        return operands

    def _emit_conditional_rhs_refs(self, value: Node, scope: RefEmitScope) -> None:
        # A boolean-default RHS binds either operand; a ternary
        # binds a RESULT operand, never the truthiness-tested
        # condition (whose index differs between Python's
        # conditional_expression and TS's ternary_expression).
        operands = (
            list(value.named_children)
            if value.type == cs.TS_PY_BOOLEAN_OPERATOR
            else conditional_result_operands(value, False)
        )
        for operand in operands:
            operand = js_calls.unwrap_ts_value(operand)
            if (
                operand.type in _ASSIGNMENT_RHS_REF_TYPES
                or operand.type in js_bindings.INLINE_FUNC_VALUE_TYPES
            ):
                self._callbacks.emit_scope_ref(scope, operand)

    @host_site_scoped
    def _emit_assigned_name_ref(
        self,
        assign_node: Node,
        caller_spec: tuple[str, str, str],
        ensure_rel: Callable[..., None],
        caller_qn: str | None,
    ) -> None:
        # Resolve the assignment target's simple name (`api.setState` ->
        # setState, `listener` -> listener) to a def-pass registration in the
        # enclosing scope. Registry-guarded: emits only when such a node exists,
        # so a plain data assignment adds nothing.
        self._host._site_node = assign_node
        if assign_node.type != cs.TS_ASSIGNMENT_EXPRESSION:
            return
        left = assign_node.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return
        name_node = (
            left.child_by_field_name(cs.FIELD_PROPERTY)
            if left.type == cs.TS_MEMBER_EXPRESSION
            else left
        )
        if name_node is None or name_node.type not in (
            cs.TS_IDENTIFIER,
            cs.TS_PROPERTY_IDENTIFIER,
        ):
            return
        if not (name := safe_decode_text(name_node)):
            return
        scope_qn = caller_qn or caller_spec[2]
        self._callbacks.reference_registered(
            caller_spec,
            [f"{scope_qn}{cs.SEPARATOR_DOT}{name}"],
            ensure_rel,
            cs.RelationshipType.REFERENCES,
        )

    @host_site_scoped
    def ingest_jsx_component_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
    ) -> None:
        """Reference the components a scope's JSX renders and the handlers
        its props hand over."""
        # `<Card />` renders the Card component: the framework invokes it
        # through the element, never by a call the graph can see, so the JSX
        # usage references the component. Uppercase names only; lowercase
        # tags are HTML elements and must not misbind to same-named locals.
        # The walk stops at nested scope boundaries (each nested function's
        # own pass covers its JSX) but continues THROUGH jsx elements so
        # nested markup is covered by the scope that renders it.
        # Resolve and emit directly rather than via emit_callback_edge: a
        # class component resolves to a CLASS node whose reference must point
        # at the class itself, but that helper redirects CLASS -> __init__
        # and drops the edge when __init__ is absent, as it always is for a
        # JS/TS class.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._host._emit_rel
        scope = RefEmitScope(
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            caller_qn,
            resolve_func,
            ensure_rel,
        )
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            # Stop at a nested scope that gets its OWN caller pass (a named
            # function/arrow, a class), but continue THROUGH an anonymous arrow
            # (a `.map()`/`cell`/forwardRef callback): those are skipped as
            # callers, so their JSX, rendered on behalf of this scope, would
            # otherwise be scanned by nobody and report as dead.
            if node.type in boundary_types and not self._scopes.is_unowned_scope(
                node, module_qn
            ):
                continue
            if node.type in _JSX_NAMED_ELEMENT_TYPES:
                self._emit_jsx_element_ref(node, scope)
            elif node.type == cs.TS_JSX_EXPRESSION:
                # A `{...}` attribute value hands its inner expression to the
                # element as a prop. A bare identifier (onClick={handleLogout})
                # or inline arrow (onClick={() => x()}) is a function the
                # framework invokes on the event, so reference it; other
                # expressions resolve to nothing and are skipped by the helper.
                # Peel ONLY a ternary / cast wrapper, whose operands are candidate
                # handler values (onDrop={cond ? this.handleDrop : undefined},
                # issue #980). A short-circuit (`x || y`, `x && y`) is NOT peeled:
                # in a JSX data prop (`alt={name || fallback}`) its operands are
                # data, and expanding them would false-revive a same-named module
                # function.
                self._emit_jsx_expression_refs(node, scope)
            stack.extend(node.children)

    def _emit_jsx_element_ref(self, node: Node, scope: RefEmitScope) -> None:
        registry = self._resolver.function_registry
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        name_text = safe_decode_text(name_node) if name_node else None
        if not name_text or not name_text[0].isupper():
            return
        # `<Prim.Item>` resolves `Prim` first: a package namespace renders the
        # package's own member. The resolver is called without a language
        # here, so the JS/TS member-call gate never sees the dotted tag and
        # the bare-name fallback would bind `Item` anywhere in the project.
        if self._resolver.js_ts.js_member_head_is_external(name_text, scope.module_qn):
            return
        resolved = scope.resolve_func(
            name_text,
            scope.module_qn,
            scope.local_var_types,
            scope.class_context,
            scope.caller_qn,
        )
        if not resolved or resolved[0] not in _JSX_COMPONENT_TARGET_TYPES:
            return
        res_type, res_qn = resolved
        for target_qn in registry.variants(res_qn):
            scope.ensure_rel(
                scope.caller_spec,
                cs.RelationshipType.REFERENCES,
                (res_type, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    def _emit_jsx_expression_refs(self, node: Node, scope: RefEmitScope) -> None:
        for value in node.named_children:
            for operand in self._jsx_value_operands(value):
                self._callbacks.emit_scope_ref(scope, operand)

    @staticmethod
    def _jsx_value_operands(value: Node) -> list[Node]:
        # The candidate handler operands of a JSX `{...}` value: a ternary's two
        # result branches (`cond ? a : b`), peeled through any transparent TS
        # cast wrapper (`(cond ? a : b) as any`). A short-circuit or any other
        # expression is returned as-is (a single operand) so it is resolved
        # directly, never peeled -- expanding `||`/`&&`/`??` in a JSX data prop
        # would false-revive a same-named module function (issue #980).
        node = value
        while node.type in cs.TS_CAST_WRAPPER_TYPES and node.named_children:
            node = node.named_children[0]
        if node.type in (cs.TS_PY_CONDITIONAL_EXPRESSION, cs.TS_JS_TERNARY_EXPRESSION):
            return conditional_result_operands(node, is_dart=False)
        return [node]

    @host_site_scoped
    def ingest_returned_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        """Reference the functions a scope hands back through `return`."""
        # A function handed back via `return` (a useEffect cleanup
        # `return () => unsubscribe()`, a factory `return handler`) is invoked by
        # whoever receives it, never by a visible call. Reference it from the
        # returning scope. Walk continues through anonymous arrows (the effect
        # callback is anonymous, so its `return` bubbles here) but stops at named
        # nested functions, which own their returns. The languages that run
        # it share the `return_statement` node type, and the emit path
        # resolves bare names and self-attributes alike.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._host._emit_rel
        # An expression-bodied arrow (`const persistImpl = (config) =>
        # (set, get, api) => {...}`, zustand's curried middleware shape) has NO
        # return_statement; its body IS the implicit return. Reference the inner
        # function directly, both on the caller itself and on any unowned anon
        # arrow the walk continues through (deeper currying bubbles here too).
        self._emit_expression_body_return(
            caller_node,
            caller_spec,
            ensure_rel,
            caller_qn,
            module_qn,
            local_var_types,
            class_context,
            resolve_func,
        )
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            if node.type in boundary_types:
                if not self._scopes.is_unowned_scope(node, module_qn):
                    continue
                self._emit_expression_body_return(
                    node,
                    caller_spec,
                    ensure_rel,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                )
            if node.type == cs.TS_RETURN_STATEMENT:
                for value in node.named_children:
                    # A returned TUPLE hides its function elements one level
                    # down (`return _load_field, (...)` in django Field's
                    # __reduce__: pickle later calls the first element);
                    # expand containers so each function handed back inside
                    # one is referenced like a bare return.
                    for expanded in self._callbacks.expand_py_first_class_values(
                        value, language
                    ):
                        self._callbacks.emit_callback_edge(
                            caller_spec,
                            expanded,
                            module_qn,
                            local_var_types,
                            class_context,
                            resolve_func,
                            ensure_rel,
                            caller_qn=caller_qn,
                            rel_type=cs.RelationshipType.REFERENCES,
                        )
            stack.extend(node.children)

    def _emit_expression_body_return(
        self,
        func_node: Node,
        caller_spec: tuple[str, str, str],
        ensure_rel: Callable[..., None],
        caller_qn: str | None,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
    ) -> None:
        body = func_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        if body.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
            self._callbacks.emit_inline_arg_function_ref(
                body,
                caller_spec,
                ensure_rel,
                caller_qn,
                cs.RelationshipType.REFERENCES,
                module_qn=module_qn,
            )
        elif body.type in _ASSIGNMENT_RHS_REF_TYPES:
            # A bare-name implicit return (`() => noop`) hands back an EXISTING
            # function by reference; resolve it like an explicit `return noop`
            # instead of treating the arrow's body as an inline declaration.
            # BUT a body rooted at one of the arrow's OWN parameters
            # (`(noop) => noop`, `(x) => x.foo`) names the parameter, which
            # shadows any same-named module function; resolving it would
            # fabricate a false edge, so skip it.
            if (
                func_node.type in js_bindings.INLINE_FUNC_VALUE_TYPES
                and self._body_root_identifier(body) in js_ts_parameter_names(func_node)
            ):
                return
            self._callbacks.emit_callback_edge(
                caller_spec,
                body,
                module_qn,
                local_var_types,
                class_context,
                resolve_func,
                ensure_rel,
                caller_qn=caller_qn,
                rel_type=cs.RelationshipType.REFERENCES,
            )

    @staticmethod
    def _body_root_identifier(node: Node) -> str | None:
        # The leading identifier of a reference body: `noop` -> "noop",
        # `x.foo.bar` / `x->foo` -> "x". Peels member/attribute/selector wrappers
        # to their object/operand so a member chain rooted at a parameter is
        # recognised. Returns None when the leftmost element is not an identifier.
        current: Node | None = node
        while current is not None:
            if current.type == cs.TS_PY_IDENTIFIER:
                return safe_decode_text(current)
            current = current.child_by_field_name(cs.FIELD_OBJECT)
        return None

    @host_site_scoped
    def ingest_default_param_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        caller_qn: str | None,
    ) -> None:
        """Reference the functions a caller's parameter defaults name."""
        # A DEFAULT PARAMETER value naming a function (`useStore(api,
        # selector = identity as any)`, zustand; Python's
        # `def run(self, conv=_via_default)`, issue #2838) references it:
        # the default is invoked through the parameter when the caller
        # omits the argument, never by a visible call.
        params = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is None:
            return
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._host._emit_rel
        for param in params.named_children:
            self._host._site_node = param
            # TS carries a param default in required_parameter's `value` field;
            # plain JS wraps the param in an assignment_pattern whose default
            # sits under `right`. Scan both forms.
            value = param.child_by_field_name(
                cs.FIELD_VALUE
            ) or param.child_by_field_name(cs.FIELD_RIGHT)
            if value is None:
                continue
            value = js_calls.unwrap_ts_value(value)
            if (
                value.type in _ASSIGNMENT_RHS_REF_TYPES
                or value.type in js_bindings.INLINE_FUNC_VALUE_TYPES
            ):
                self._callbacks.emit_callback_edge(
                    caller_spec,
                    value,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                    caller_qn,
                    cs.RelationshipType.REFERENCES,
                )
