from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .utils import type_identifier_text


class GoTypeInferenceEngine:
    # Maps local variable / parameter / receiver names to their bare Go type
    # name within a function or method body, so the resolver can bind a
    # receiver-dispatch call (`d.method()`) to the method node on the type.
    # Bare names only: the resolver turns a name into a class qn via the same
    # _resolve_class_name path the definition pass uses, so pointer/generic
    # wrappers are stripped here down to the underlying type identifier.
    __slots__ = ()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        var_types: dict[str, str] = {}
        self._collect_receiver(caller_node, var_types)
        self._collect_parameters(caller_node, var_types)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_body_declarations(body, var_types)
        return var_types

    def build_field_type_map(self, class_node: Node) -> dict[str, str]:
        # Map a Go struct's field names to their bare type names (a `type_spec`:
        # `type Engine struct { trees methodTrees }` -> {"trees": "methodTrees"}).
        # Lets the resolver type a field-hop receiver (`engine.trees.get()`), so a
        # local bound from such a call (`root := engine.trees.get(m)`) gets the
        # return type. Non-struct type_specs (aliases, interfaces) yield {}.
        fields: dict[str, str] = {}
        struct = next(
            (c for c in class_node.children if c.type == cs.TS_GO_STRUCT_TYPE), None
        )
        if struct is None:
            return fields
        field_list = next(
            (c for c in struct.children if c.type == cs.TS_GO_FIELD_DECLARATION_LIST),
            None,
        )
        if field_list is None:
            return fields
        for decl in field_list.children:
            if decl.type != cs.TS_GO_FIELD_DECLARATION:
                continue
            type_node = decl.child_by_field_name(cs.FIELD_TYPE)
            if type_node is None or not (type_name := type_identifier_text(type_node)):
                continue
            for child in decl.children:
                if child.type == cs.TS_GO_FIELD_IDENTIFIER and (
                    name := safe_decode_text(child)
                ):
                    fields[name] = type_name
        return fields

    def _collect_receiver(self, caller_node: Node, var_types: dict[str, str]) -> None:
        receiver = caller_node.child_by_field_name(cs.FIELD_RECEIVER)
        if receiver is not None:
            self._collect_parameter_list(receiver, var_types)

    def _collect_parameters(self, caller_node: Node, var_types: dict[str, str]) -> None:
        params = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is not None:
            self._collect_parameter_list(params, var_types)

    def _collect_parameter_list(
        self, list_node: Node, var_types: dict[str, str]
    ) -> None:
        for param in list_node.children:
            if param.type != cs.TS_GO_PARAMETER_DECLARATION:
                continue
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            if type_node is None or not (type_name := type_identifier_text(type_node)):
                continue
            for child in param.children:
                if child.type == cs.TS_IDENTIFIER and (name := safe_decode_text(child)):
                    var_types[name] = type_name

    def _collect_body_declarations(self, node: Node, var_types: dict[str, str]) -> None:
        match node.type:
            case cs.TS_GO_VAR_DECLARATION:
                self._collect_var_declaration(node, var_types)
            case cs.TS_GO_SHORT_VAR_DECLARATION:
                self._collect_short_var_declaration(node, var_types)
            case _:
                pass
        for child in node.children:
            self._collect_body_declarations(child, var_types)

    def _collect_var_declaration(self, node: Node, var_types: dict[str, str]) -> None:
        # `var a, b T` binds every name in the spec to the declared type.
        for spec in node.children:
            if spec.type != cs.TS_GO_VAR_SPEC:
                continue
            type_node = spec.child_by_field_name(cs.FIELD_TYPE)
            if type_node is None or not (type_name := type_identifier_text(type_node)):
                continue
            for child in spec.children:
                if child.type == cs.TS_IDENTIFIER and (name := safe_decode_text(child)):
                    var_types[name] = type_name

    def _collect_short_var_declaration(
        self, node: Node, var_types: dict[str, str]
    ) -> None:
        # `x := T{}` / `x := &T{}`: pair each left name with the type inferred
        # from the value at the same position; non-literal initializers (calls)
        # are left unresolved rather than guessed.
        left = node.child_by_field_name(cs.FIELD_LEFT)
        right = node.child_by_field_name(cs.FIELD_RIGHT)
        if left is None or right is None:
            return
        names = [
            safe_decode_text(c) for c in left.children if c.type == cs.TS_IDENTIFIER
        ]
        values = [c for c in right.children if c.is_named]
        for name, value in zip(names, values, strict=False):
            if name and (type_name := self._infer_value_type(value)):
                var_types[name] = type_name

    def collect_call_var_bindings(
        self, caller_node: Node
    ) -> list[tuple[str, list[str]]]:
        # `x := recv.m(...)` / `x := e.field.m(...)` / `x := f(...)`: pair the bound
        # name with the callee selector segments (`e.trees.get` -> ['e','trees',
        # 'get']). The unified engine resolves the segments to the call's return
        # type (needs field + method-return maps this stateless engine lacks) and
        # types `x`. Only clean identifier-dotted callees are collected; anything
        # with an index/paren/generic in the callee is skipped (stays unresolved).
        bindings: list[tuple[str, list[str]]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is not None:
            self._collect_call_bindings(body, bindings)
        return bindings

    def _collect_call_bindings(
        self, node: Node, bindings: list[tuple[str, list[str]]]
    ) -> None:
        if node.type == cs.TS_GO_SHORT_VAR_DECLARATION:
            self._collect_call_binding(node, bindings)
        for child in node.children:
            self._collect_call_bindings(child, bindings)

    def _collect_call_binding(
        self, node: Node, bindings: list[tuple[str, list[str]]]
    ) -> None:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        right = node.child_by_field_name(cs.FIELD_RIGHT)
        if left is None or right is None:
            return
        names = [
            safe_decode_text(c) for c in left.children if c.type == cs.TS_IDENTIFIER
        ]
        values = [c for c in right.children if c.is_named]
        for name, value in zip(names, values, strict=False):
            if not name or value.type != cs.TS_GO_CALL_EXPRESSION:
                continue
            if segments := self._callee_segments(value):
                bindings.append((name, segments))

    def _callee_segments(self, call: Node) -> list[str] | None:
        # The callee selector of a call, split into identifier segments. A
        # plain function is one segment; `e.trees.get` is three. Returns None for
        # any non-identifier part (index/paren/generic) so callers stay unresolved.
        func = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_IDENTIFIER:
            return [safe_decode_text(func) or ""] if func.text else None
        if func.type != cs.TS_GO_SELECTOR_EXPRESSION:
            return None
        segments: list[str] = []
        current: Node | None = func
        while current is not None and current.type == cs.TS_GO_SELECTOR_EXPRESSION:
            field = current.child_by_field_name(cs.FIELD_FIELD)
            operand = current.child_by_field_name(cs.FIELD_OPERAND)
            if field is None or field.type != cs.TS_GO_FIELD_IDENTIFIER:
                return None
            segments.append(safe_decode_text(field) or "")
            current = operand
        if current is None or current.type != cs.TS_IDENTIFIER or not current.text:
            return None
        segments.append(safe_decode_text(current) or "")
        segments.reverse()
        return segments if all(segments) else None

    def _infer_value_type(self, value: Node) -> str | None:
        if value.type == cs.TS_GO_COMPOSITE_LITERAL:
            type_node = value.child_by_field_name(cs.FIELD_TYPE)
            return type_identifier_text(type_node) if type_node else None
        if value.type == cs.TS_GO_UNARY_EXPRESSION:
            # `&T{}` wraps the composite literal in its operand.
            operand = value.child_by_field_name(cs.FIELD_OPERAND)
            return self._infer_value_type(operand) if operand else None
        if value.type == cs.TS_GO_TYPE_ASSERTION_EXPRESSION:
            # `x := y.(T)` / `y.(*T)` (gin's `c := pool.Get().(*Context)`): the
            # asserted type is x's type, so a later field-hop / method call on x
            # resolves. type_identifier_text unwraps the `*T` pointer.
            type_node = value.child_by_field_name(cs.FIELD_TYPE)
            return type_identifier_text(type_node) if type_node else None
        return None
