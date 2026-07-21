from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .utils import tuple_group_inner


class RustTypeInferenceEngine:
    # Maps local names (parameters, `let` bindings, enum-match variant bindings)
    # to their bare Rust type name within a function/method body, so the resolver
    # can bind a receiver-dispatch call (`cmd.apply()`) to the method on the type
    # instead of guessing via the ambiguous name-only trie fallback. Directly
    # knowable types only; call-return bindings (`let x = T::new()`) are collected
    # separately and typed by the unified engine (which has the return-type map).
    __slots__ = ()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        var_types: dict[str, str] = {}
        self._collect_parameters(caller_node, var_types)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_bindings(body, var_types)
        return var_types

    def build_field_type_map(self, class_node: Node) -> dict[str, str]:
        # Map a Rust struct's field names to their bare type names
        # (`struct Handler { shutdown: Shutdown }` -> {"shutdown": "Shutdown"}),
        # so a field-hop receiver (`self.shutdown.is_shutdown()`) resolves.
        fields: dict[str, str] = {}
        field_list = class_node.child_by_field_name(cs.FIELD_BODY)
        if field_list is None or field_list.type != cs.TS_RS_FIELD_DECLARATION_LIST:
            return fields
        for decl in field_list.children:
            if decl.type != cs.TS_RS_FIELD_DECLARATION:
                continue
            name_node = decl.child_by_field_name(cs.FIELD_NAME)
            type_node = decl.child_by_field_name(cs.FIELD_TYPE)
            if name_node is None or type_node is None:
                continue
            if (name := safe_decode_text(name_node)) and (
                type_name := self._bare_type_name(type_node)
            ):
                fields[name] = type_name
        return fields

    def build_field_guard_inner_map(self, class_node: Node) -> dict[str, str]:
        # For struct fields whose type is a guard container (`state: Mutex<State>`),
        # record field -> inner type (`state` -> State). The field map itself keeps
        # the WRAPPER (`Mutex`), so a direct `self.state.is_poisoned()` resolves
        # against the wrapper (correct); the inner is applied ONLY when a chain
        # reaches a lock/read/borrow guard accessor. Guard containers do not
        # deref-coerce, so this is the only sound place to unwrap.
        inners: dict[str, str] = {}
        field_list = class_node.child_by_field_name(cs.FIELD_BODY)
        if field_list is None or field_list.type != cs.TS_RS_FIELD_DECLARATION_LIST:
            return inners
        for decl in field_list.children:
            if decl.type != cs.TS_RS_FIELD_DECLARATION:
                continue
            name_node = decl.child_by_field_name(cs.FIELD_NAME)
            type_node = decl.child_by_field_name(cs.FIELD_TYPE)
            if name_node is None or type_node is None:
                continue
            name = safe_decode_text(name_node)
            if name and (inner := self._guard_inner_type(type_node)):
                inners[name] = inner
        return inners

    def _guard_inner_type(self, type_node: Node) -> str | None:
        # `Mutex<State>` / `Arc<Mutex<State>>` -> State; None for a non-guard type.
        # A guard wrapped in a deref pointer (`Arc<Mutex<T>>`) still unwraps to the
        # guard's inner, so peel deref pointers first.
        if type_node.type != cs.TS_GENERIC_TYPE:
            return None
        outer = type_node.child_by_field_name(cs.FIELD_TYPE)
        outer_name = safe_decode_text(outer) if outer else None
        args = type_node.child_by_field_name(cs.TS_RS_TYPE_ARGUMENTS)
        inner = (
            next(
                (c for c in args.children if c.type in cs.RS_RETURN_TYPE_NODE_TYPES),
                None,
            )
            if args is not None
            else None
        )
        if inner is None:
            return None
        if outer_name in cs.RS_GUARD_WRAPPERS:
            return self._bare_type_name(inner)
        if outer_name in cs.RS_DEREF_WRAPPERS:
            return self._guard_inner_type(inner)
        return None

    def collect_call_var_bindings(
        self, caller_node: Node
    ) -> list[tuple[str, list[str]]]:
        # `let x = Type::assoc(...)` / `let x = Type::assoc(...).unwrap()`: pair the
        # bound name with the callee chain segments (base type first, then method
        # hops: `['Command', 'from_frame']`). The unified engine walks the segments
        # through the return-type map to type `x`. Only type-rooted associated-call
        # chains are collected; anything else stays unresolved.
        bindings: list[tuple[str, list[str]]] = []
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_call_bindings(body, bindings)
        return bindings

    def _collect_parameters(self, caller_node: Node, var_types: dict[str, str]) -> None:
        params = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is None:
            return
        for param in params.children:
            if param.type != cs.TS_RS_PARAMETER:
                continue
            pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            if pattern is None or pattern.type != cs.TS_IDENTIFIER or type_node is None:
                continue
            if (name := safe_decode_text(pattern)) and (
                type_name := self._bare_type_name(type_node)
            ):
                var_types[name] = type_name

    def _collect_bindings(self, node: Node, var_types: dict[str, str]) -> None:
        # Only `let` bindings go in the flat map. Match-variant bindings are NOT
        # flattened here: a shared name across arms (or a nested match rebinding a
        # param) would clobber the flat entry with the wrong (last) type. They are
        # supplied per-arm-scoped via collect_match_arm_bindings and overlaid by the
        # resolver at each call's position instead.
        if node.type == cs.TS_RS_LET_DECLARATION:
            self._collect_let_binding(node, var_types)
        for child in node.children:
            self._collect_bindings(child, var_types)

    def _collect_let_binding(self, node: Node, var_types: dict[str, str]) -> None:
        # `let x: T = ...` (explicit annotation) and `let x = T { .. }` (struct
        # literal) yield a directly-known type. `let x = T::assoc(..)` is left for
        # collect_call_var_bindings (needs the return-type map).
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        if pattern is None or pattern.type != cs.TS_IDENTIFIER:
            return
        name = safe_decode_text(pattern)
        if not name:
            return
        if annotation := node.child_by_field_name(cs.FIELD_TYPE):
            if type_name := self._bare_type_name(annotation):
                var_types[name] = type_name
            return
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if value is not None and value.type == cs.TS_RS_STRUCT_EXPRESSION:
            struct_name = value.child_by_field_name(cs.FIELD_NAME)
            if struct_name and (type_name := self._bare_type_name(struct_name)):
                var_types[name] = type_name

    def _tuple_struct_binding(self, pattern: Node) -> tuple[str, str] | None:
        # `Variant(x)`: bind x to the variant's payload type. Rust's newtype idiom
        # (`Command::Get(Get)`) names the variant after the wrapped type, so the
        # variant name IS the payload type. Only single-field patterns bind (a
        # multi-field variant has no single payload type).
        variant = pattern.child_by_field_name(cs.FIELD_TYPE)
        if variant is None:
            return None
        variant_name = self._path_leaf_name(variant)
        bound = [
            c for c in pattern.children if c.type == cs.TS_IDENTIFIER and c != variant
        ]
        if variant_name and len(bound) == 1 and (name := safe_decode_text(bound[0])):
            return (name, variant_name)
        return None

    def collect_match_arm_bindings(
        self, caller_node: Node
    ) -> list[tuple[int, int, str, str]]:
        # Per-arm scoped match-variant bindings: (arm_start_byte, arm_end_byte,
        # binding_name, variant_type). Lets the resolver overlay the binding whose
        # arm range contains a call, so each `cmd.apply()` in a distinct arm
        # resolves to its OWN variant type instead of the flat map's last-arm one.
        bindings: list[tuple[int, int, str, str]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return bindings
        for arm in self._descendants_of_type(body, cs.TS_RS_MATCH_ARM):
            # Extract only THIS arm's own pattern bindings, not descendants: a
            # nested `match` lives in the arm's value/body and is a separate
            # match_arm (collected in its own iteration with its own range).
            # Scanning all descendants would scope a nested arm's binding to the
            # whole outer arm, mis-overlaying outer-scope calls.
            arm_pattern = arm.child_by_field_name(cs.TS_FIELD_PATTERN)
            if arm_pattern is None:
                continue
            for pattern in self._descendants_of_type(
                arm_pattern, cs.TS_RS_TUPLE_STRUCT_PATTERN
            ):
                if binding := self._tuple_struct_binding(pattern):
                    bindings.append((arm.start_byte, arm.end_byte, *binding))
        return bindings

    def _collect_call_bindings(
        self, node: Node, bindings: list[tuple[str, list[str]]]
    ) -> None:
        if node.type == cs.TS_RS_LET_DECLARATION:
            self._collect_call_binding(node, bindings)
        for child in node.children:
            self._collect_call_bindings(child, bindings)

    def _collect_call_binding(
        self, node: Node, bindings: list[tuple[str, list[str]]]
    ) -> None:
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if pattern is None or pattern.type != cs.TS_IDENTIFIER or value is None:
            return
        name = safe_decode_text(pattern)
        if not name:
            return
        value_expr = self._unwrap_try(value)
        if segments := self._callee_chain_segments(value_expr):
            # A single bare identifier is a move or fn-pointer binding
            # (`let f = make;`), not a call: `f` holds the function itself,
            # not a value of its return type. Only an invoked base counts.
            if len(segments) == 1 and value_expr.type not in cs.RS_CALL_OR_GENERIC_FN:
                return
            bindings.append((name, segments))

    def _callee_chain_segments(self, node: Node) -> list[str] | None:
        # Flatten a Rust value expression into ordered chain segments, base first:
        # `Type::assoc().m()` -> ['Type','assoc','m']; `self.shared.state.lock()
        # .unwrap()` -> ['self','shared','state','lock','unwrap']. Method calls and
        # field accesses are both segments (the resolver disambiguates each hop as
        # field/method/identity). Base must be an identifier, `self`, or a scoped
        # `Type::assoc` path; anything else (index, literal) yields None.
        node = self._unwrap_try(node)
        if node.type in cs.RS_CALL_OR_GENERIC_FN:
            # generic_function is turbofish (`f::<T>()`); descend to its callee.
            func = node.child_by_field_name(cs.FIELD_FUNCTION)
            return self._callee_chain_segments(func) if func is not None else None
        if node.type == cs.TS_RS_FIELD_EXPRESSION:
            receiver = node.child_by_field_name(cs.FIELD_VALUE)
            field = node.child_by_field_name(cs.FIELD_FIELD)
            field_name = safe_decode_text(field) if field else None
            if receiver is None or not field_name:
                return None
            if base := self._callee_chain_segments(receiver):
                return [*base, field_name]
            return None
        if node.type == cs.TS_SCOPED_IDENTIFIER:
            path = node.child_by_field_name(cs.TS_RS_FIELD_PATH)
            name = node.child_by_field_name(cs.FIELD_NAME)
            # Keep the FULL base path (`crate::cmd::Command`) so a fully-qualified
            # inline call disambiguates by path in the return-type lookup.
            base = safe_decode_text(path) if path else None
            leaf = safe_decode_text(name) if name else None
            return [base, leaf] if base and leaf else None
        if node.type in cs.RS_IDENT_OR_SELF:
            return [text] if (text := safe_decode_text(node)) else None
        return None

    def _unwrap_try(self, node: Node) -> Node:
        # `expr?` is a try_expression wrapping the real value.
        while node.type == cs.TS_RS_TRY_EXPRESSION and node.child_count:
            node = node.children[0]
        return node

    def _bare_type_name(self, type_node: Node) -> str | None:
        return _rust_bare_type_name(type_node)

    def _path_leaf_name(self, node: Node) -> str | None:
        # Last identifier of a path: `Command` from a bare identifier,
        # `Unknown` from `Command::Unknown`.
        if node.type in cs.RS_IDENTIFIER_TYPES:
            return safe_decode_text(node)
        if node.type in cs.RS_SCOPED_TYPES:
            name = node.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name) if name else None
        return None

    def _descendants_of_type(self, node: Node, node_type: str) -> list[Node]:
        found: list[Node] = []

        def walk(n: Node) -> None:
            if n.type == node_type:
                found.append(n)
            for child in n.children:
                walk(child)

        walk(node)
        return found


def _rust_bare_generic_name(type_node: Node) -> str | None:
    outer = type_node.child_by_field_name(cs.FIELD_TYPE)
    outer_name = _rust_bare_type_name(outer) if outer else None
    # A receiver type strips only transparent deref pointers (Arc<Shared>
    # -> Shared); Option/Result/Vec are kept (a call dispatches to them).
    if outer_name not in cs.RS_DEREF_WRAPPERS:
        return outer_name
    args = type_node.child_by_field_name(cs.TS_RS_TYPE_ARGUMENTS)
    if args is None:
        return outer_name
    inner = next(
        (c for c in args.children if c.type in cs.RS_RETURN_TYPE_NODE_TYPES),
        None,
    )
    return _rust_bare_type_name(inner) if inner else outer_name


def _rust_bare_type_name(type_node: Node) -> str | None:
    # Bare type name, stripping references/generics/wrappers down to the leaf
    # identifier: `&'a mut Shutdown` -> Shutdown, `Result<Command>` -> Command.
    match type_node.type:
        case cs.TS_TYPE_IDENTIFIER | cs.TS_RS_PRIMITIVE_TYPE:
            return safe_decode_text(type_node)
        case cs.TS_GENERIC_TYPE:
            return _rust_bare_generic_name(type_node)
        case cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            name = type_node.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name) if name else None
        case cs.TS_RS_TUPLE_TYPE:
            inner = tuple_group_inner(type_node)
            return _rust_bare_type_name(inner) if inner else None
        case _:
            # reference_type / dyn / impl / bounded / other wrappers: descend
            # to the first typed child (a bounded type's first bound is the
            # principal trait; the rest are auto-trait markers).
            for child in type_node.children:
                if child.type in cs.RS_RETURN_TYPE_NODE_TYPES:
                    return _rust_bare_type_name(child)
            return None
