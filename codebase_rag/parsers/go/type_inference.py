from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .utils import type_identifier_text


class GoTypeInferenceEngine:
    # (H) Maps local variable / parameter / receiver names to their bare Go type
    # (H) name within a function or method body, so the resolver can bind a
    # (H) receiver-dispatch call (`d.method()`) to the method node on the type.
    # (H) Bare names only: the resolver turns a name into a class qn via the same
    # (H) _resolve_class_name path the definition pass uses, so pointer/generic
    # (H) wrappers are stripped here down to the underlying type identifier.
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
        # (H) `var a, b T` binds every name in the spec to the declared type.
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
        # (H) `x := T{}` / `x := &T{}`: pair each left name with the type inferred
        # (H) from the value at the same position; non-literal initializers (calls)
        # (H) are left unresolved rather than guessed.
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

    def _infer_value_type(self, value: Node) -> str | None:
        if value.type == cs.TS_GO_COMPOSITE_LITERAL:
            type_node = value.child_by_field_name(cs.FIELD_TYPE)
            return type_identifier_text(type_node) if type_node else None
        if value.type == cs.TS_GO_UNARY_EXPRESSION:
            # (H) `&T{}` wraps the composite literal in its operand.
            operand = value.child_by_field_name(cs.FIELD_OPERAND)
            return self._infer_value_type(operand) if operand else None
        return None
