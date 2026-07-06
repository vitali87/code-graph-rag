from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


class RustTypeInferenceEngine:
    # (H) Maps local names (parameters, `let` bindings, enum-match variant bindings)
    # (H) to their bare Rust type name within a function/method body, so the resolver
    # (H) can bind a receiver-dispatch call (`cmd.apply()`) to the method on the type
    # (H) instead of guessing via the ambiguous name-only trie fallback. Directly
    # (H) knowable types only; call-return bindings (`let x = T::new()`) are collected
    # (H) separately and typed by the unified engine (which has the return-type map).
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
        # (H) Map a Rust struct's field names to their bare type names
        # (H) (`struct Handler { shutdown: Shutdown }` -> {"shutdown": "Shutdown"}),
        # (H) so a field-hop receiver (`self.shutdown.is_shutdown()`) resolves.
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

    def collect_call_var_bindings(
        self, caller_node: Node
    ) -> list[tuple[str, list[str]]]:
        # (H) `let x = Type::assoc(...)` / `let x = Type::assoc(...).unwrap()`: pair the
        # (H) bound name with the callee chain segments (base type first, then method
        # (H) hops: `['Command', 'from_frame']`). The unified engine walks the segments
        # (H) through the return-type map to type `x`. Only type-rooted associated-call
        # (H) chains are collected; anything else stays unresolved.
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
        # (H) Only `let` bindings go in the flat map. Match-variant bindings are NOT
        # (H) flattened here: a shared name across arms (or a nested match rebinding a
        # (H) param) would clobber the flat entry with the wrong (last) type. They are
        # (H) supplied per-arm-scoped via collect_match_arm_bindings and overlaid by the
        # (H) resolver at each call's position instead.
        if node.type == cs.TS_RS_LET_DECLARATION:
            self._collect_let_binding(node, var_types)
        for child in node.children:
            self._collect_bindings(child, var_types)

    def _collect_let_binding(self, node: Node, var_types: dict[str, str]) -> None:
        # (H) `let x: T = ...` (explicit annotation) and `let x = T { .. }` (struct
        # (H) literal) yield a directly-known type. `let x = T::assoc(..)` is left for
        # (H) collect_call_var_bindings (needs the return-type map).
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
        # (H) `Variant(x)`: bind x to the variant's payload type. Rust's newtype idiom
        # (H) (`Command::Get(Get)`) names the variant after the wrapped type, so the
        # (H) variant name IS the payload type. Only single-field patterns bind (a
        # (H) multi-field variant has no single payload type).
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
        # (H) Per-arm scoped match-variant bindings: (arm_start_byte, arm_end_byte,
        # (H) binding_name, variant_type). Lets the resolver overlay the binding whose
        # (H) arm range contains a call, so each `cmd.apply()` in a distinct arm
        # (H) resolves to its OWN variant type instead of the flat map's last-arm one.
        bindings: list[tuple[int, int, str, str]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return bindings
        for arm in self._descendants_of_type(body, cs.TS_RS_MATCH_ARM):
            # (H) Extract only THIS arm's own pattern bindings, not descendants: a
            # (H) nested `match` lives in the arm's value/body and is a separate
            # (H) match_arm (collected in its own iteration with its own range).
            # (H) Scanning all descendants would scope a nested arm's binding to the
            # (H) whole outer arm, mis-overlaying outer-scope calls.
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
        if segments := self._callee_chain_segments(self._unwrap_try(value)):
            bindings.append((name, segments))

    def _callee_chain_segments(self, node: Node) -> list[str] | None:
        # (H) A `Type::assoc(..).m1().m2()` call, flattened to type-then-methods:
        # (H) `['Type', 'assoc', 'm1', 'm2']`. Returns None unless the chain roots in a
        # (H) `Type::assoc` associated-function call (the only shape we can type).
        if node.type != cs.TS_RS_CALL_EXPRESSION:
            return None
        func = node.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_SCOPED_IDENTIFIER:
            path = func.child_by_field_name(cs.TS_RS_FIELD_PATH)
            method = func.child_by_field_name(cs.FIELD_NAME)
            # (H) Keep the FULL base path (`crate::cmd::Command`), not just the leaf, so
            # (H) a fully-qualified inline call (`crate::cmd::Command::from_frame()`)
            # (H) with no `use` import disambiguates by path in the return-type lookup.
            base = safe_decode_text(path) if path else None
            method_name = safe_decode_text(method) if method else None
            if base and method_name:
                return [base, method_name]
            return None
        if func.type == cs.TS_RS_FIELD_EXPRESSION:
            receiver = func.child_by_field_name(cs.FIELD_VALUE)
            method = func.child_by_field_name(cs.FIELD_FIELD)
            method_name = safe_decode_text(method) if method else None
            if receiver is None or not method_name:
                return None
            if base_segments := self._callee_chain_segments(self._unwrap_try(receiver)):
                return [*base_segments, method_name]
        return None

    def _unwrap_try(self, node: Node) -> Node:
        # (H) `expr?` is a try_expression wrapping the real value.
        while node.type == cs.TS_RS_TRY_EXPRESSION and node.child_count:
            node = node.children[0]
        return node

    def _bare_type_name(self, type_node: Node) -> str | None:
        return _rust_bare_type_name(type_node)

    def _path_leaf_name(self, node: Node) -> str | None:
        # (H) Last identifier of a path: `Command` from a bare identifier,
        # (H) `Unknown` from `Command::Unknown`.
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


def _rust_bare_type_name(type_node: Node) -> str | None:
    # (H) Bare type name, stripping references/generics/wrappers down to the leaf
    # (H) identifier: `&'a mut Shutdown` -> Shutdown, `Result<Command>` -> Command.
    match type_node.type:
        case cs.TS_TYPE_IDENTIFIER | cs.TS_RS_PRIMITIVE_TYPE:
            return safe_decode_text(type_node)
        case cs.TS_GENERIC_TYPE:
            outer = type_node.child_by_field_name(cs.FIELD_TYPE)
            outer_name = _rust_bare_type_name(outer) if outer else None
            # (H) A receiver type strips only transparent deref pointers (Arc<Shared>
            # (H) -> Shared); Option/Result/Vec are kept (a call dispatches to them).
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
        case cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            name = type_node.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name) if name else None
        case _:
            # (H) reference_type / other wrappers: descend to the first typed child.
            for child in type_node.children:
                if child.type in cs.RS_RETURN_TYPE_NODE_TYPES:
                    return _rust_bare_type_name(child)
            return None
