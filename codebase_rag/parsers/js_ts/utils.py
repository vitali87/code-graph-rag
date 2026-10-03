from collections.abc import Mapping
from typing import TYPE_CHECKING

from tree_sitter import Language, Node, QueryCursor

from ... import constants as cs
from ..utils import get_cached_query, safe_decode_text

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries


def get_js_ts_language_obj(
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, "LanguageQueries"],
) -> Language | None:
    if language not in cs.JS_TS_LANGUAGES:
        return None

    lang_queries = queries[language]
    return lang_queries.get(cs.QUERY_LANGUAGE)


def _extract_class_qn(method_qn: str) -> str | None:
    qn_parts = method_qn.split(cs.SEPARATOR_DOT)
    return cs.SEPARATOR_DOT.join(qn_parts[:-1]) if len(qn_parts) >= 2 else None


def extract_method_call(member_expr_node: Node) -> str | None:
    object_node = member_expr_node.child_by_field_name(cs.FIELD_OBJECT)
    property_node = member_expr_node.child_by_field_name(cs.FIELD_PROPERTY)

    if object_node and property_node:
        object_text = object_node.text
        property_text = property_node.text

        if object_text and property_text:
            object_name = safe_decode_text(object_node)
            property_name = safe_decode_text(property_node)
            return f"{object_name}{cs.SEPARATOR_DOT}{property_name}"

    return None


def find_method_in_class_body(class_body_node: Node, method_name: str) -> Node | None:
    for child in class_body_node.children:
        if child.type == cs.TS_METHOD_DEFINITION:
            name_node = child.child_by_field_name(cs.FIELD_NAME)
            if name_node and name_node.text:
                found_name = safe_decode_text(name_node)
                if found_name == method_name:
                    return child

    return None


def repeats_member_signature(member_node: Node, seen_names: set[str]) -> bool:
    # An overloaded interface or abstract member is one signature per overload,
    # but a single member: only the first names it, so a later one registering
    # would mint an `@line` duplicate no call can bind and dead code reports.
    # `seen_names` is the caller's per-body record of names already declared.
    if member_node.type not in cs.TS_BODILESS_METHOD_TYPES:
        return False
    name = safe_decode_text(member_node.child_by_field_name(cs.FIELD_NAME))
    if name is None:
        return False
    if name in seen_names:
        return True
    seen_names.add(name)
    return False


_ANNOTATED_PARAMETER_TYPES = frozenset(
    {cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER}
)


def annotated_parameter_types(function_node: Node) -> list[tuple[str, str]]:
    """(name, written type name) for each plainly named, annotated parameter.

    The type name is the one a member call can dispatch on: `Repo`, the head of
    `Repo<T>`, or the one non-nullish member of `Repo | undefined`. A
    destructured parameter, or any other type shape, is left out.
    """
    parameters = function_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if parameters is None:
        return []
    found: list[tuple[str, str]] = []
    for parameter in parameters.named_children:
        if parameter.type not in _ANNOTATED_PARAMETER_TYPES:
            continue
        pattern = parameter.child_by_field_name(cs.TS_FIELD_PATTERN)
        annotation = parameter.child_by_field_name(cs.FIELD_TYPE)
        if pattern is None or pattern.type != cs.TS_IDENTIFIER or annotation is None:
            continue
        name = safe_decode_text(pattern)
        type_name = _dispatch_type_name(next(iter(annotation.named_children), None))
        if name and type_name:
            found.append((name, type_name))
    return found


def _dispatch_type_name(type_node: Node | None) -> str | None:
    if type_node is not None and type_node.type == cs.TS_UNION_TYPE:
        members = [
            member
            for member in type_node.named_children
            if (safe_decode_text(member) or "") not in cs.TS_NULLISH_TYPE_TEXTS
        ]
        type_node = members[0] if len(members) == 1 else None
    if type_node is not None and type_node.type == cs.TS_GENERIC_TYPE:
        type_node = type_node.child_by_field_name(cs.FIELD_NAME)
    if type_node is None or type_node.type != cs.TS_TYPE_IDENTIFIER:
        return None
    return safe_decode_text(type_node)


_CLASS_BODY_CACHE: dict[str, Node | None] = {}
# The OWNER is a strong reference, never a bare id(): a freed tree's heap
# address gets recycled, so an integer owner could masquerade as current and
# serve Node values from a dead tree (the xdist worker-distribution flake,
# issue #1042). Holding the reference pins the owner's tree, so no live tree
# can alias its address — which is what makes NODE EQUALITY sound here, and
# equality (not identity) is required because each `tree.root_node` access
# mints a fresh wrapper object over the same tree node. Pins exactly one
# tree, the one the cache describes.
_CLASS_BODY_CACHE_OWNER: Node | None = None


def find_method_in_ast(
    root_node: Node, class_name: str, method_name: str
) -> Node | None:
    global _CLASS_BODY_CACHE_OWNER
    if _CLASS_BODY_CACHE_OWNER != root_node:
        _CLASS_BODY_CACHE.clear()
        _CLASS_BODY_CACHE_OWNER = root_node
    cache_key = class_name
    if cache_key in _CLASS_BODY_CACHE:
        body_node = _CLASS_BODY_CACHE[cache_key]
        if body_node is not None:
            return find_method_in_class_body(body_node, method_name)
        return None

    declaration = _class_declaration_named(root_node, class_name)
    body_node = (
        declaration.child_by_field_name(cs.FIELD_BODY)
        if declaration is not None
        else None
    )
    _CLASS_BODY_CACHE[cache_key] = body_node
    if body_node:
        return find_method_in_class_body(body_node, method_name)
    return None


def _class_declaration_named(root_node: Node, class_name: str) -> Node | None:
    # The first `class_declaration` named `class_name`, in source order.
    stack: list[Node] = [root_node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_CLASS_DECLARATION:
            name_node = current.child_by_field_name(cs.FIELD_NAME)
            if (
                name_node
                and name_node.text
                and safe_decode_text(name_node) == class_name
            ):
                return current
        stack.extend(reversed(current.children))
    return None


_JS_RETURN_QUERY = "(return_statement) @return_stmt"


def find_return_statements(
    node: Node, return_nodes: list[Node], language_obj=None
) -> None:
    if language_obj is not None:
        try:
            q = get_cached_query(language_obj, _JS_RETURN_QUERY)
            cursor = QueryCursor(q)
            captures = cursor.captures(node)
            return_nodes.extend(captures.get("return_stmt", []))
            return
        except Exception:  # noqa: S110 - a failed query falls back to the walk below
            pass
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_RETURN_STATEMENT:
            return_nodes.append(current)
        stack.extend(reversed(current.children))


def extract_constructor_name(new_expr_node: Node) -> str | None:
    if new_expr_node.type != cs.TS_NEW_EXPRESSION:
        return None

    constructor_node = new_expr_node.child_by_field_name(cs.FIELD_CONSTRUCTOR)
    if constructor_node and constructor_node.type == cs.TS_IDENTIFIER:
        constructor_text = constructor_node.text
        if constructor_text:
            return safe_decode_text(constructor_node)

    return None


_BINDING_WRAPPER_TYPES = cs.TS_CAST_WRAPPER_TYPES | {cs.TS_PARENTHESIZED_EXPRESSION}


def arrow_binding_name(func_node: Node) -> str | None:
    # An arrow / function expression has no `name` field. Recover the binding
    # name for the two named forms whose VALUE is the arrow: a module/local
    # `const f = () => ...` (variable_declarator) and a class field
    # `helper = () => ...` (public_field_definition). Both the definition pass
    # (registering the arrow's qn) and the call pass (attributing the body's
    # calls) must derive the SAME name, or the caller qn is a phantom and the
    # arrow's body callbacks report dead; sharing this one helper keeps them in
    # step. Anonymous / destructured arrows, and arrows that are merely an
    # argument to a call bound to a name (`const m = useMutation(() => {})`),
    # stay unnamed: the arrow is not the binding's own value there.
    if func_node.type not in (
        cs.TS_ARROW_FUNCTION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION,
    ):
        return None
    return _value_binding_name(func_node)


def _value_binding_name(node: Node) -> str | None:
    # Recover the name a nameless expression is bound to: the `name` of the
    # variable_declarator / public_field_definition whose `value` is this node.
    # The node may sit behind transparent wrappers (parens, TS casts:
    # `export const create = ((s) => ...) as Create`); climb them first so the
    # node is recognised as the binding's value.
    parent = node.parent
    while parent is not None and parent.type in _BINDING_WRAPPER_TYPES:
        node = parent
        parent = node.parent
    if parent is None:
        return None
    # `==` not `is`: py-tree-sitter returns a fresh Node wrapper per access, so
    # identity comparison always fails.
    if parent.child_by_field_name(cs.FIELD_VALUE) != node:
        return None
    name_node = parent.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.type not in (
        cs.TS_IDENTIFIER,
        cs.TS_PROPERTY_IDENTIFIER,
    ):
        return None
    return safe_decode_text(name_node)


def class_binding_name(class_node: Node) -> str | None:
    # An anonymous CLASS EXPRESSION (`static Proxy = class {...}`,
    # `const Proxy = class {...}`) has no `name` field. Recover the field /
    # declarator binding name so its methods attribute to `Outer.Proxy.<method>`
    # and are enumerated as callers; without it the class expression is skipped
    # in the caller pass and every callback inside its methods reports dead
    # (issue #970). A NAMED class expression (`class Named {}`) keeps its own
    # name. Non-class nodes and unbound anonymous classes (`foo(class {})`)
    # return None, matching today's skip.
    if class_node.type != cs.TS_CLASS_EXPRESSION:
        return None
    name_node = class_node.child_by_field_name(cs.FIELD_NAME)
    if name_node is not None and name_node.text:
        return safe_decode_text(name_node)
    return _value_binding_name(class_node)


def is_object_literal_method(func_node: Node) -> bool:
    # `{delay () {...}}` (getters and setters too): a method written in an
    # object literal, which binds no name in any scope.
    parent = func_node.parent
    return (
        func_node.type == cs.TS_METHOD_DEFINITION
        and parent is not None
        and parent.type == cs.TS_OBJECT
    )


def analyze_return_expression(expr_node: Node, method_qn: str) -> str | None:
    match expr_node.type:
        case cs.TS_NEW_EXPRESSION:
            if class_name := extract_constructor_name(expr_node):
                return _extract_class_qn(method_qn) or class_name
            return None

        case cs.TS_THIS:
            return _extract_class_qn(method_qn)

        case cs.TS_MEMBER_EXPRESSION:
            object_node = expr_node.child_by_field_name(cs.FIELD_OBJECT)
            if not object_node:
                return None

            match object_node.type:
                case cs.TS_THIS:
                    return _extract_class_qn(method_qn)
                case cs.TS_IDENTIFIER:
                    if object_node.text:
                        object_name = safe_decode_text(object_node)
                        qn_parts = method_qn.split(cs.SEPARATOR_DOT)
                        if len(qn_parts) >= 2 and object_name == qn_parts[-2]:
                            return cs.SEPARATOR_DOT.join(qn_parts[:-1])
            return None

        case _:
            return None
