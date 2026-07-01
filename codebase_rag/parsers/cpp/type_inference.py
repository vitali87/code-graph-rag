from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


class CppTypeInferenceEngine:
    # (H) Maps local variable / parameter names to their bare C++ type name within a
    # (H) function or method body, so the resolver can bind a member-dispatch call
    # (H) (`obj->method()` / `obj.method()`) to the method node on the receiver's
    # (H) class instead of guessing by the bare method name. Bare names only: the
    # (H) resolver turns a name into a class qn via the same _resolve_class_name path
    # (H) the definition pass uses, so pointer/reference/const/template wrappers are
    # (H) stripped here down to the underlying type identifier.
    __slots__ = ()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        var_types: dict[str, str] = {}
        if declarator := self._function_declarator(caller_node):
            self._collect_parameters(declarator, var_types)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_body_declarations(body, var_types)
        return var_types

    def _function_declarator(self, caller_node: Node) -> Node | None:
        # (H) The parameter_list hangs off the (possibly pointer/reference-wrapped)
        # (H) function_declarator in the definition's declarator chain.
        declarator = caller_node.child_by_field_name(cs.FIELD_DECLARATOR)
        while declarator is not None:
            if declarator.type == cs.CppNodeType.FUNCTION_DECLARATOR:
                return declarator
            declarator = declarator.child_by_field_name(cs.FIELD_DECLARATOR)
        return None

    def _collect_parameters(self, declarator: Node, var_types: dict[str, str]) -> None:
        params = declarator.child_by_field_name(cs.KEY_PARAMETERS)
        if params is None:
            return
        for param in params.children:
            if param.type not in (
                cs.CppNodeType.PARAMETER_DECLARATION,
                cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
            ):
                continue
            self._record_declaration(param, var_types)

    def _collect_body_declarations(self, node: Node, var_types: dict[str, str]) -> None:
        for child in node.children:
            if child.type == cs.CppNodeType.DECLARATION:
                self._record_declaration(child, var_types)
            # (H) Recurse into nested blocks (if/for/while/try bodies) so a variable
            # (H) declared in an inner scope still resolves; last write wins, which is
            # (H) good enough for the single-type-per-name model this map supports.
            self._collect_body_declarations(child, var_types)

    def _record_declaration(self, node: Node, var_types: dict[str, str]) -> None:
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or not (type_name := self._bare_type_name(type_node)):
            return
        declarator = node.child_by_field_name(cs.FIELD_DECLARATOR)
        if (name := self._declarator_name(declarator)) is not None:
            var_types[name] = type_name

    def _bare_type_name(self, type_node: Node) -> str | None:
        match type_node.type:
            case cs.CppNodeType.TYPE_IDENTIFIER:
                return safe_decode_text(type_node)
            case cs.CppNodeType.QUALIFIED_IDENTIFIER:
                # (H) `ns::Foo` -> `Foo`: the resolver maps the bare class name to its
                # (H) namespaced node qn via find_ending_with.
                return self._rightmost_name(type_node)
            case cs.CppNodeType.TEMPLATE_TYPE:
                inner = type_node.child_by_field_name(cs.KEY_NAME)
                return self._bare_type_name(inner) if inner is not None else None
            case _:
                return None

    def _rightmost_name(self, node: Node) -> str | None:
        name_node = node.child_by_field_name(cs.KEY_NAME)
        if name_node is not None and name_node.type in (
            cs.CppNodeType.TYPE_IDENTIFIER,
            cs.CppNodeType.IDENTIFIER,
        ):
            return safe_decode_text(name_node)
        text = safe_decode_text(node)
        if not text:
            return None
        return text.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1] or None

    def _declarator_name(self, declarator: Node | None) -> str | None:
        # (H) Unwrap pointer/reference/init declarators down to the bound identifier.
        current = declarator
        while current is not None:
            if current.type == cs.CppNodeType.IDENTIFIER:
                return safe_decode_text(current)
            if inner := current.child_by_field_name(cs.FIELD_DECLARATOR):
                current = inner
                continue
            return None
        return None
