from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from .utils import dart_body_node


class DartTypeInferenceEngine:
    # (H) Dart receiver typing (analog of the C#/Java engines): type function
    # (H) parameters and body locals so the generic local-type resolution can
    # (H) bind `g.greet()` to the receiver's class method instead of leaving
    # (H) it to the suffix trie's arbitrary pick among same-named candidates.

    def build_local_variable_type_map(self, caller_node: Node) -> dict[str, str]:
        # (H) caller_node is the SIGNATURE (the grammar splits the body off as
        # (H) a sibling): parameters come from the signature's parameter list,
        # (H) locals from the sibling body. Conflicting redefinitions of one
        # (H) name (sibling blocks reusing a binding) drop, mirroring the C#
        # (H) engine's conservative rule.
        types: dict[str, str] = {}
        conflicts: set[str] = set()
        self._collect_parameters(caller_node, types, conflicts)
        body = dart_body_node(caller_node)
        if body is not None:
            self._collect_locals(body, types, conflicts)
        return types

    def build_field_type_map(self, class_node: Node) -> dict[str, str]:
        # (H) `String name;` in a class_body is declaration(type_identifier,
        # (H) initialized_identifier_list); record {name: String} so a
        # (H) field-typed receiver (`buddy.greet()`, `this.buddy.hail()`)
        # (H) resolves through the field's declared type.
        fields: dict[str, str] = {}
        for child in class_node.named_children:
            if child.type != cs.TS_DART_CLASS_BODY:
                continue
            for member in child.named_children:
                if member.type != cs.TS_DART_DECLARATION:
                    continue
                self._record_field(member, fields)
        return fields

    @staticmethod
    def _record_field(member: Node, fields: dict[str, str]) -> None:
        type_name: str | None = None
        for part in member.named_children:
            if part.type == cs.TS_DART_TYPE_IDENTIFIER and part.text:
                type_name = part.text.decode(cs.ENCODING_UTF8)
            elif part.type == cs.TS_DART_INITIALIZED_IDENTIFIER_LIST and type_name:
                for entry in part.named_children:
                    if entry.type != cs.TS_DART_INITIALIZED_IDENTIFIER:
                        continue
                    for ident in entry.named_children:
                        if ident.type == cs.TS_DART_IDENTIFIER and ident.text:
                            fields[ident.text.decode(cs.ENCODING_UTF8)] = type_name
                        break

    def _collect_parameters(
        self, caller_node: Node, types: dict[str, str], conflicts: set[str]
    ) -> None:
        stack = [caller_node]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_DART_FORMAL_PARAMETER:
                self._record_parameter(node, types, conflicts)
                continue
            stack.extend(node.named_children)

    @staticmethod
    def _record_parameter(
        node: Node, types: dict[str, str], conflicts: set[str]
    ) -> None:
        type_name: str | None = None
        for part in node.named_children:
            if part.type == cs.TS_DART_TYPE_IDENTIFIER and part.text:
                type_name = part.text.decode(cs.ENCODING_UTF8)
            elif part.type == cs.TS_DART_IDENTIFIER and part.text and type_name:
                _record(part.text.decode(cs.ENCODING_UTF8), type_name, types, conflicts)

    def _collect_locals(
        self, body: Node, types: dict[str, str], conflicts: set[str]
    ) -> None:
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            if node.type == cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION:
                self._record_local(node, types, conflicts)
                continue
            stack.extend(node.named_children)

    @staticmethod
    def _record_local(node: Node, types: dict[str, str], conflicts: set[str]) -> None:
        # (H) Two typable shapes: a DECLARED type (`Greeter t = ...` puts a
        # (H) type_identifier before the name) and a CONSTRUCTION initializer
        # (H) (`var b = Greeter('x')` / `final n = Greeter.named('y')`: the
        # (H) initializer's base identifier, UpperCamelCase by Dart
        # (H) convention, names the constructed class; a lowercase base is an
        # (H) ordinary call whose return type is unknown, so the local stays
        # (H) untyped). The FIRST variable's name and initializer are direct
        # (H) children; each ADDITIONAL variable of a multi-declaration
        # (H) (`var a = X(), b = Y();`) nests as an initialized_identifier
        # (H) carrying the same name-plus-initializer shape, with a declared
        # (H) type (if any) shared by every binding.
        declared_type: str | None = None
        for part in node.named_children:
            if part.type == cs.TS_DART_TYPE_IDENTIFIER and part.text:
                declared_type = part.text.decode(cs.ENCODING_UTF8)
                break
        _record_binding(node.named_children, declared_type, types, conflicts)
        for part in node.named_children:
            if part.type == cs.TS_DART_INITIALIZED_IDENTIFIER:
                _record_binding(part.named_children, declared_type, types, conflicts)


def _record_binding(
    parts: list[Node],
    declared_type: str | None,
    types: dict[str, str],
    conflicts: set[str],
) -> None:
    # (H) One name-plus-initializer run: the first identifier is the variable,
    # (H) a second identifier followed by an argument selector is a
    # (H) construction base typing the variable when no declared type applies.
    var_name: str | None = None
    init_base: str | None = None
    has_argument_selector = False
    for part in parts:
        if part.type == cs.TS_DART_IDENTIFIER and part.text:
            if var_name is None:
                var_name = part.text.decode(cs.ENCODING_UTF8)
            elif init_base is None:
                init_base = part.text.decode(cs.ENCODING_UTF8)
        elif part.type == cs.TS_DART_SELECTOR and any(
            inner.type == cs.TS_DART_ARGUMENT_PART for inner in part.named_children
        ):
            has_argument_selector = True
    if var_name is None:
        return
    if declared_type is not None:
        _record(var_name, declared_type, types, conflicts)
        return
    if init_base is not None and has_argument_selector and init_base[:1].isupper():
        _record(var_name, init_base, types, conflicts)


def _record(
    name: str, type_name: str, types: dict[str, str], conflicts: set[str]
) -> None:
    if name in conflicts:
        return
    if name in types and types[name] != type_name:
        conflicts.add(name)
        del types[name]
        return
    types[name] = type_name
