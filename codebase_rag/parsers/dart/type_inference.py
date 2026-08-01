from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from .utils import _selector_member_name, dart_body_node


class DartTypeInferenceEngine:
    # Dart receiver typing (analog of the C#/Java engines): type function
    # parameters and body locals so the generic local-type resolution can
    # bind `g.greet()` to the receiver's class method instead of leaving
    # it to the suffix trie's arbitrary pick among same-named candidates.

    def build_local_variable_type_map(self, caller_node: Node) -> dict[str, str]:
        # caller_node is the SIGNATURE (the grammar splits the body off as
        # a sibling): parameters come from the signature's parameter list,
        # locals from the sibling body. Conflicting redefinitions of one
        # name (sibling blocks reusing a binding) drop, mirroring the C#
        # engine's conservative rule.
        types: dict[str, str] = {}
        conflicts: set[str] = set()
        self._collect_parameters(caller_node, types, conflicts)
        body = dart_body_node(caller_node)
        if body is not None:
            self._collect_locals(body, types, conflicts)
        return types

    def collect_static_call_bindings(
        self, caller_node: Node
    ) -> dict[str, tuple[str, str]]:
        # Locals bound from a class-qualified call (`var s =
        # Base.member(args)`, an UpperCamelCase base): the construction
        # heuristic types them as Base, but a registered member's RECORDED
        # return type should win (a `static String describe()` local is a
        # String, not the class). The hub enrichment consumes this map.
        # OWN-scope only (PR #807 review): a nested function's same-named
        # binding must never retype the outer local, and since the
        # enrichment cannot tell which scope produced a var_types entry, a
        # nested binding is never collected at all -- nested locals simply
        # keep the construction heuristic. A definition with an EXPLICIT
        # declared type is skipped entirely: the declaration statically
        # fixes the type and the initializer's return must not override
        # or untype it.
        bindings: dict[str, tuple[str, str]] = {}
        body = dart_body_node(caller_node)
        if body is None:
            return bindings
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            if node.type in cs.DART_NESTED_SCOPE_NODE_TYPES:
                continue
            if node.type == cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION:
                self._record_definition_calls(node, bindings)
                continue
            stack.extend(node.named_children)
        return bindings

    @staticmethod
    def _record_definition_calls(
        node: Node, bindings: dict[str, tuple[str, str]]
    ) -> None:
        if _declared_type_name(node) is not None:
            return
        _record_static_call(node.named_children, bindings)
        for part in node.named_children:
            if part.type == cs.TS_DART_INITIALIZED_IDENTIFIER:
                _record_static_call(part.named_children, bindings)

    def build_field_type_map(self, class_node: Node) -> dict[str, str]:
        # `String name;` in a class_body is declaration(type_identifier,
        # initialized_identifier_list); record {name: String} so a
        # field-typed receiver (`buddy.greet()`, `this.buddy.hail()`)
        # resolves through the field's declared type.
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
        type_name = _declared_type_name(member)
        if type_name is None:
            return
        for part in member.named_children:
            if part.type == cs.TS_DART_INITIALIZED_IDENTIFIER_LIST:
                _record_field_names(part, type_name, fields)

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
        # Two passes with precedence (PR #806 review): the caller's OWN
        # scope first, with full conflict semantics; then nested
        # function/lambda scopes, fill-in only. Nested locals must still
        # be collected -- a Dart test body is a lambda argument
        # (`test('x', () { var p = ArgParser(); p.addFlag(...); })`) whose
        # calls flat-attribute to the enclosing caller -- but an inner
        # same-named local must never conflict-drop or overwrite the
        # outer binding.
        nested: list[Node] = []
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            if node.type in cs.DART_NESTED_SCOPE_NODE_TYPES:
                nested.append(node)
                continue
            if node.type == cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION:
                self._record_local(node, types, conflicts)
                continue
            stack.extend(node.named_children)
        fill_in: dict[str, str] = {}
        fill_conflicts: set[str] = set(conflicts)
        stack = nested
        while stack:
            node = stack.pop()
            if node.type == cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION:
                self._record_local(node, fill_in, fill_conflicts)
                continue
            stack.extend(node.named_children)
        for name, type_name in fill_in.items():
            if name not in types and name not in conflicts:
                types[name] = type_name

    @staticmethod
    def _record_local(node: Node, types: dict[str, str], conflicts: set[str]) -> None:
        # Two typable shapes: a DECLARED type (`Greeter t = ...` puts a
        # type_identifier before the name) and a CONSTRUCTION initializer
        # (`var b = Greeter('x')` / `final n = Greeter.named('y')`: the
        # initializer's UpperCamelCase base identifier names the constructed
        # class; a lowercase base is an ordinary call of unknown return type,
        # so the local stays untyped). The FIRST variable's name and
        # initializer are direct children; each ADDITIONAL variable of a
        # multi-declaration (`var a = X(), b = Y();`) nests as an
        # initialized_identifier of the same shape, with any declared type
        # shared by every binding.
        declared_type = _declared_type_name(node)
        _record_binding(node.named_children, declared_type, types, conflicts)
        for part in node.named_children:
            if part.type == cs.TS_DART_INITIALIZED_IDENTIFIER:
                _record_binding(part.named_children, declared_type, types, conflicts)


def _declared_type_name(node: Node) -> str | None:
    for part in node.named_children:
        if part.type == cs.TS_DART_TYPE_IDENTIFIER and part.text:
            return part.text.decode(cs.ENCODING_UTF8)
    return None


def _record_field_names(id_list: Node, type_name: str, fields: dict[str, str]) -> None:
    # only an entry's FIRST identifier is the field name; later
    # identifiers belong to its initializer
    for entry in id_list.named_children:
        if entry.type != cs.TS_DART_INITIALIZED_IDENTIFIER:
            continue
        for ident in entry.named_children:
            if ident.type == cs.TS_DART_IDENTIFIER and ident.text:
                fields[ident.text.decode(cs.ENCODING_UTF8)] = type_name
            break


def _record_binding(
    parts: list[Node],
    declared_type: str | None,
    types: dict[str, str],
    conflicts: set[str],
) -> None:
    # One name-plus-initializer run: the first identifier is the variable,
    # a second identifier followed by an argument selector is a
    # construction base typing the variable when no declared type applies.
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


def _record_static_call(
    parts: list[Node], bindings: dict[str, tuple[str, str]]
) -> None:
    # shape: identifier var, identifier Base, selector(.member),
    # selector(argument_part); anything else is not a class-qualified call
    var_name: str | None = None
    base: str | None = None
    member: str | None = None
    has_argument_selector = False
    for part in parts:
        if part.type == cs.TS_DART_IDENTIFIER and part.text:
            if var_name is None:
                var_name = part.text.decode(cs.ENCODING_UTF8)
            elif base is None:
                base = part.text.decode(cs.ENCODING_UTF8)
        elif part.type == cs.TS_DART_SELECTOR:
            if any(
                inner.type == cs.TS_DART_ARGUMENT_PART for inner in part.named_children
            ):
                has_argument_selector = True
            elif member is None:
                member = _selector_member_name(part)
    if (
        var_name is not None
        and base is not None
        and member is not None
        and has_argument_selector
        and base[:1].isupper()
    ):
        bindings[var_name] = (base, member)


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
