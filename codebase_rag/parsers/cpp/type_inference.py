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
        decls: list[tuple[str, str]] = []
        if declarator := self._function_declarator(caller_node):
            self._collect_parameters(declarator, decls)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_body_declarations(body, decls)
        # (H) The map is keyed by name only, with no knowledge of a call's lexical
        # (H) position, so it cannot tell an outer `Zeta z` from an inner-block
        # (H) `Alpha z` that shadows it. Rather than pick a write order that is wrong
        # (H) for one of the two scopes, decline to infer any name declared with more
        # (H) than one type: such a call falls back to name-only resolution instead of
        # (H) getting a confidently wrong typed edge. (Same flat-map limitation the Go
        # (H) engine carries; true scoping would need positional call resolution.)
        var_types: dict[str, str] = {}
        conflicting: set[str] = set()
        for name, type_name in decls:
            if name in conflicting:
                continue
            existing = var_types.get(name)
            if existing is not None and existing != type_name:
                del var_types[name]
                conflicting.add(name)
                continue
            var_types[name] = type_name
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

    def _collect_parameters(
        self, declarator: Node, decls: list[tuple[str, str]]
    ) -> None:
        params = declarator.child_by_field_name(cs.KEY_PARAMETERS)
        if params is None:
            return
        for param in params.children:
            if param.type not in (
                cs.CppNodeType.PARAMETER_DECLARATION,
                cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
            ):
                continue
            self._record_declaration(param, decls)

    def _collect_body_declarations(
        self, node: Node, decls: list[tuple[str, str]]
    ) -> None:
        for child in node.children:
            # (H) A lambda / nested function / local class body opens its own scope;
            # (H) its declarations are not locals of the enclosing function, so descend
            # (H) no further or an inner `x` would be attributed to the outer `x`.
            if child.type in cs.CPP_NESTED_SCOPE_NODE_TYPES:
                continue
            if child.type == cs.CppNodeType.DECLARATION:
                self._record_declaration(child, decls)
            # (H) Recurse into ordinary nested blocks (if/for/while/try bodies) so a
            # (H) variable declared only in an inner block still resolves; conflicting
            # (H) redecls across scopes are reconciled by the caller (drop-on-conflict).
            self._collect_body_declarations(child, decls)

    def _record_declaration(self, node: Node, decls: list[tuple[str, str]]) -> None:
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or not (type_name := self._bare_type_name(type_node)):
            return
        # (H) One statement may declare several variables sharing the leading type
        # (H) (`Zeta a, b;`), each its own `declarator` field child; record them all.
        for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR):
            if (name := self._declarator_name(declarator)) is not None:
                decls.append((name, type_name))

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
            # (H) `reference_declarator` (`T& x`) holds its identifier as a positional
            # (H) child, not under the `declarator` field that pointer/init declarators
            # (H) expose, so the field-based unwrap stalls; descend into the first
            # (H) named declarator-bearing child instead.
            current = self._first_declarator_child(current)
        return None

    def _first_declarator_child(self, node: Node) -> Node | None:
        for child in node.children:
            if child.type in (
                cs.CppNodeType.IDENTIFIER,
                cs.CppNodeType.REFERENCE_DECLARATOR,
                cs.CppNodeType.POINTER_DECLARATOR,
                cs.CppNodeType.INIT_DECLARATOR,
            ):
                return child
        return None
