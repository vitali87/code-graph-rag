"""Dispatch tables: the functions a scope's dict, object, list and array
literals hold as values.

A function placed in a literal is wired to be invoked later
(`handlers[key](...)`), often through a dynamic key or from another module
where no call site resolves statically. The `TableReferences` the call pass
builds references each such value from the scope that builds the table,
peeling the wrappers a value hides in and finding an inline function by the
name or position the definition pass registered it under. The reference
walkers share its host protocol (`ReferenceHost`) and the scopes a walk goes
on through (`UnownedScopes`)."""

from collections.abc import Callable
from typing import Protocol

from tree_sitter import Node

from .. import constants as cs
from ..node_text import safe_decode_text
from .call_callbacks import (
    FLOW_ARG_REF_TYPES,
    PY_SEQUENCE_LITERAL_TYPES,
    CallbackEdges,
    RefEmitScope,
    scoped_candidates,
)
from .call_host import CallStrategyHost
from .call_resolver import CallResolver
from .edge_site import EdgeWriterHost, host_site_scoped
from .js_ts import bindings as js_bindings
from .js_ts import calls as js_calls

# Dispatch-table literals whose values may name handler functions: Python dict
# and JS/TS object (key/value pairs), Python list/set/tuple and JS/TS array
# (positional elements). All use the `pair`/named-child shapes handled below.
_DICT_LIKE_COLLECTION_TYPES = frozenset({cs.TS_PY_DICTIONARY, cs.TS_OBJECT})
_SEQUENCE_LIKE_COLLECTION_TYPES = PY_SEQUENCE_LITERAL_TYPES | frozenset({cs.TS_ARRAY})


class UnownedScopes(Protocol):
    """Which nested scopes get no caller pass of their own, so a walk goes
    on through them: an anonymous JS/TS arrow or function expression."""

    def is_unowned_scope(self, node: Node, module_qn: str) -> bool: ...


class ReferenceHost(EdgeWriterHost, CallStrategyHost, Protocol):
    """What the reference walkers call on the call pass that builds them: the
    edge writer, and the call strategy of the language a scope is written in.
    Beside the edge site, which each walk sets to the node it visits."""


class TableReferences:
    """The walk that references the functions a scope's literals hold.

    Built and owned by `CallProcessor`; a value resolves through the pass's
    resolver, in the scope of the walk that finds it."""

    __slots__ = ("_host", "_callbacks", "_resolver", "_scopes")

    def __init__(
        self,
        host: ReferenceHost,
        callbacks: CallbackEdges,
        resolver: CallResolver,
        scopes: UnownedScopes,
    ) -> None:
        self._host = host
        self._callbacks = callbacks
        self._resolver = resolver
        self._scopes = scopes

    @host_site_scoped
    def ingest_collection_function_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        """Reference the functions a scope's dict, object, list and array
        literals hold as values."""
        # A function/method placed as a value in a dict/object or list/array literal
        # is a dispatch table wired to be invoked later (handlers[key](...)),
        # commonly dispatched by a dynamic string key or in another module where the
        # call site is not statically resolvable. Treat each such reference as a call
        # from the enclosing scope so the handler is reachable. The walk stops at
        # nested function/class boundaries, so a table built inside a nested scope
        # is attributed to that scope's own pass, EXCEPT an unowned JS/TS arrow (a
        # Promise executor, a `.forEach` callback), which gets no caller pass; its
        # calls bubble to this scope, so its object tables (a `defineProperty`
        # getter descriptor) must be scanned here too or the callbacks inside are
        # orphaned and report as dead.
        resolve_func = self._resolver.resolve_function_call
        ensure_rel = self._host.emit_rel
        scope = RefEmitScope(
            caller_spec,
            module_qn,
            local_var_types,
            class_context,
            caller_spec[2],
            resolve_func,
            ensure_rel,
        )
        # Containers an enclosing literal's expansion already peeled. Each
        # expansion covers the whole nested literal, so expanding every
        # nested level again as the walk reached it made a deeply nested
        # literal O(n^2) (#2262); the walk still descends into them.
        opened: set[Node] = set()
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            if node.type in boundary_types and not self._scopes.is_unowned_scope(
                node, module_qn
            ):
                continue
            if node.type in _DICT_LIKE_COLLECTION_TYPES:
                for pair in node.named_children:
                    self._host._site_node = pair
                    self._ingest_dict_entry_ref(node, pair, scope, language, opened)
            elif node.type in _SEQUENCE_LIKE_COLLECTION_TYPES and node not in opened:
                for element in node.named_children:
                    self._emit_expanded_value_refs(element, scope, language, opened)
            stack.extend(node.children)

    def _ingest_dict_entry_ref(
        self,
        node: Node,
        pair: Node,
        scope: RefEmitScope,
        language: cs.SupportedLanguage | None,
        opened: set[Node],
    ) -> None:
        # An object-literal SHORTHAND METHOD (`return { then(x)
        # {...}, catch(x) {...} }`, persist's thenable) is a stored
        # callable like a pair value, but it is a method_definition
        # node, not a pair; reference it by name so it is not dead
        # unless the repo never calls it AND never hands the object
        # out (it cannot know the consumer).
        if pair.type == cs.TS_METHOD_DEFINITION:
            self._emit_shorthand_method_ref(
                pair, scope.caller_spec, scope.module_qn, scope.ensure_rel
            )
            return
        # A SHORTHAND property (`{ retryStrategy }` == `retryStrategy:
        # retryStrategy`): the identifier is both key and value, so it
        # can name a first-class function value to reference. ponytail:
        # a local data var shadowing a same-named module function is
        # over-referenced (the resolver is name-based), matching the
        # existing explicit-pair path; a per-scope shadow check is the
        # upgrade path if it ever matters.
        if pair.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER:
            self._callbacks.emit_scope_ref(scope, pair)
            return
        if (
            pair.type == cs.TS_PY_PAIR
            and (value := pair.child_by_field_name(cs.FIELD_VALUE)) is not None
        ):
            if value.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
                self._emit_inline_value_function_ref(
                    pair, value, scope.caller_spec, scope.module_qn, scope.ensure_rel
                )
                return
            # A table VALUE wrapped in parens or a ternary
            # (django SQLCompiler's `"local_setter": (partial(...)
            # if ... else local_setter_noop)`) hides the handler
            # candidates one level down; expand before emitting.
            if node in opened:
                return
            self._emit_expanded_value_refs(value, scope, language, opened)

    def _emit_expanded_value_refs(
        self,
        value: Node,
        scope: RefEmitScope,
        language: cs.SupportedLanguage | None,
        opened: set[Node],
    ) -> None:
        for expanded in self._callbacks.expand_py_first_class_values(
            value, language, opened
        ):
            self.emit_value_function_ref(
                expanded,
                scope.caller_spec,
                scope.module_qn,
                scope.local_var_types,
                scope.class_context,
                scope.resolve_func,
                scope.ensure_rel,
                scope.caller_qn,
            )

    @host_site_scoped
    def emit_value_function_ref(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
    ) -> None:
        """Reference the function a value in a table or sequence names."""
        # A value cast for typing (`persistImpl as unknown as Persist`) is
        # transparent for reference resolution, and `fn.bind(ctx)` /
        # `fn.call(...)` / `fn.apply(...)` in value position (onError:
        # handleError.bind(toast)) hands off `fn`; peel both to a fixpoint.
        self._host._site_node = node
        node = js_calls.peel_bound_callable(node)
        # An inline arrow/function-expression element (`handlers = [() => {}]`,
        # zod's `onattach = [(inst) => {...}]`) is registered anonymously in the
        # enclosing scope and named after no identifier, so resolve_func cannot
        # find it; reference it by position exactly like an inline call argument.
        if node.type in js_bindings.INLINE_FUNC_VALUE_TYPES:
            self._callbacks.emit_inline_arg_function_ref(
                node,
                caller_spec,
                ensure_rel,
                caller_spec[2],
                cs.RelationshipType.REFERENCES,
                module_qn,
            )
            return
        # Only a bare name / attribute / member-expression in value position names
        # a function; a call, comprehension or literal is not a reference to a
        # callable. Reuses the flow-arg ref types (identifier, Python attribute,
        # Go selector, JS/TS member expression).
        if node.type not in FLOW_ARG_REF_TYPES:
            return
        # Resolved in the enclosing scope, as a call or a callback argument
        # is: a function declared a few lines above binds before a same-named
        # one in an unimported module (issue #3199).
        self._callbacks.emit_callback_edge(
            caller_spec,
            node,
            module_qn,
            local_var_types,
            class_context,
            resolve_func,
            ensure_rel,
            caller_qn,
        )

    @host_site_scoped
    def _emit_inline_value_function_ref(
        self,
        pair: Node,
        value: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        # An inline arrow/function-expression object value is registered by the
        # definition pass under {enclosing_scope}.<name>. An identifier key names it
        # by the key (scope.onSuccess); a string-literal key ({'onSuccess': ...})
        # has no property name, so it registers as scope.anonymous_<row>_<col> from
        # the value's position. Reference every candidate actually registered
        # (variants cover same-name duplicates in one scope).
        self._host._site_node = value
        names = [f"{cs.PREFIX_ANONYMOUS}{value.start_point[0]}_{value.start_point[1]}"]
        key_node = pair.child_by_field_name(cs.FIELD_KEY)
        if (
            key_node is not None
            and key_node.type in (cs.TS_PROPERTY_IDENTIFIER, cs.TS_IDENTIFIER)
            and (key := safe_decode_text(key_node))
        ):
            names.append(key)
            # A value nested under ANOTHER pair-arrow (`{ onCreated: (s) => {
            # s.setEvents({ compute: ... }) } }`) registers under the pair-key
            # PATH (scope.onCreated.compute); prefix the ancestor pair keys so
            # the candidate matches the def pass's qn.
            if pair_path := self._ancestor_pair_key_path(pair):
                names.append(f"{pair_path}{cs.SEPARATOR_DOT}{key}")
        # A NAMED function expression value (`get: function getrouter() {...}`,
        # express) registers by its OWN name; neither the key nor the position
        # form matches it, so try the name under the scope and module-flat
        # (where the def pass puts it).
        name_node = value.child_by_field_name(cs.FIELD_NAME)
        own = safe_decode_text(name_node) if name_node is not None else None
        if own:
            names.append(own)
        candidates = scoped_candidates(caller_spec[2], names)
        if own:
            candidates.add(f"{module_qn}{cs.SEPARATOR_DOT}{own}")
        self._callbacks.reference_registered(
            caller_spec, candidates, ensure_rel, cs.RelationshipType.REFERENCES
        )

    def _ancestor_pair_key_path(self, pair: Node) -> str | None:
        # Dotted key path of the ENCLOSING pairs above this one (`compute` inside
        # `onCreated: (s) => ...` -> "onCreated"), outermost first; None when the
        # pair has no pair ancestors. Registry-guarded by the caller, so an
        # over-collected path (ancestors above the scanning scope) just misses.
        keys: list[str] = []
        current = pair.parent
        while current is not None:
            if current.type == cs.TS_PY_PAIR:
                key_node = current.child_by_field_name(cs.FIELD_KEY)
                if (
                    key_node is not None
                    and key_node.type in (cs.TS_PROPERTY_IDENTIFIER, cs.TS_IDENTIFIER)
                    and (key := safe_decode_text(key_node))
                ):
                    keys.append(key)
            current = current.parent
        if not keys:
            return None
        keys.reverse()
        return cs.SEPARATOR_DOT.join(keys)

    @host_site_scoped
    def _emit_shorthand_method_ref(
        self,
        method_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        # The def pass registers a shorthand method by NAME at MODULE scope
        # (persist's thenable `catch` -> `...middleware.persist.catch`), while
        # this scan runs per enclosing caller; try both scopes, plus the
        # position form used when the name is taken.
        self._host._site_node = method_node
        name_node = method_node.child_by_field_name(cs.FIELD_NAME)
        names = [
            f"{cs.PREFIX_ANONYMOUS}"
            f"{method_node.start_point[0]}_{method_node.start_point[1]}"
        ]
        name = safe_decode_text(name_node) if name_node is not None else None
        if name:
            names.append(name)
        candidates = scoped_candidates(caller_spec[2], names)
        if name:
            candidates.add(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
        self._callbacks.reference_registered(
            caller_spec, candidates, ensure_rel, cs.RelationshipType.REFERENCES
        )
