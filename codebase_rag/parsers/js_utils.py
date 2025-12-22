from tree_sitter import Node

from ..constants import (
    FIELD_BODY,
    FIELD_CONSTRUCTOR,
    FIELD_NAME,
    FIELD_OBJECT,
    FIELD_PROPERTY,
    SEPARATOR_DOT,
    TS_CLASS_DECLARATION,
    TS_IDENTIFIER,
    TS_MEMBER_EXPRESSION,
    TS_METHOD_DEFINITION,
    TS_NEW_EXPRESSION,
    TS_RETURN_STATEMENT,
    TS_THIS,
)
from .utils import safe_decode_text


def extract_js_method_call(member_expr_node: Node) -> str | None:
    try:
        object_node = member_expr_node.child_by_field_name(FIELD_OBJECT)
        property_node = member_expr_node.child_by_field_name(FIELD_PROPERTY)

        if object_node and property_node:
            object_text = object_node.text
            property_text = property_node.text

            if object_text and property_text:
                object_name = safe_decode_text(object_node)
                property_name = safe_decode_text(property_node)
                return f"{object_name}{SEPARATOR_DOT}{property_name}"
    except Exception:
        return None

    return None


def find_js_method_in_class_body(
    class_body_node: Node, method_name: str
) -> Node | None:
    for child in class_body_node.children:
        if child.type == TS_METHOD_DEFINITION:
            name_node = child.child_by_field_name(FIELD_NAME)
            if name_node and name_node.text:
                found_name = safe_decode_text(name_node)
                if found_name == method_name:
                    return child

    return None


def find_js_method_in_ast(
    root_node: Node, class_name: str, method_name: str
) -> Node | None:
    stack: list[Node] = [root_node]

    while stack:
        current = stack.pop()

        if current.type == TS_CLASS_DECLARATION:
            name_node = current.child_by_field_name(FIELD_NAME)
            if name_node and name_node.text:
                found_class_name = safe_decode_text(name_node)
                if found_class_name == class_name:
                    body_node = current.child_by_field_name(FIELD_BODY)
                    if body_node:
                        return find_js_method_in_class_body(body_node, method_name)

        stack.extend(reversed(current.children))

    return None


def find_js_return_statements(node: Node, return_nodes: list[Node]) -> None:
    stack: list[Node] = [node]

    while stack:
        current = stack.pop()

        if current.type == TS_RETURN_STATEMENT:
            return_nodes.append(current)

        stack.extend(reversed(current.children))


def extract_js_constructor_name(new_expr_node: Node) -> str | None:
    if new_expr_node.type != TS_NEW_EXPRESSION:
        return None

    constructor_node = new_expr_node.child_by_field_name(FIELD_CONSTRUCTOR)
    if constructor_node and constructor_node.type == TS_IDENTIFIER:
        constructor_text = constructor_node.text
        if constructor_text:
            return safe_decode_text(constructor_node)

    return None


def analyze_js_return_expression(expr_node: Node, method_qn: str) -> str | None:
    if expr_node.type == TS_NEW_EXPRESSION:
        class_name = extract_js_constructor_name(expr_node)
        if class_name:
            qn_parts = method_qn.split(SEPARATOR_DOT)
            if len(qn_parts) >= 2:
                return SEPARATOR_DOT.join(qn_parts[:-1])
            return class_name

    elif expr_node.type == TS_THIS:
        qn_parts = method_qn.split(SEPARATOR_DOT)
        if len(qn_parts) >= 2:
            return SEPARATOR_DOT.join(qn_parts[:-1])

    elif expr_node.type == TS_MEMBER_EXPRESSION:
        object_node = expr_node.child_by_field_name(FIELD_OBJECT)
        if object_node:
            if object_node.type == TS_THIS:
                qn_parts = method_qn.split(SEPARATOR_DOT)
                if len(qn_parts) >= 2:
                    return SEPARATOR_DOT.join(qn_parts[:-1])
            elif object_node.type == TS_IDENTIFIER:
                object_text = object_node.text
                if object_text:
                    object_name = safe_decode_text(object_node)
                    qn_parts = method_qn.split(SEPARATOR_DOT)
                    if len(qn_parts) >= 2 and object_name == qn_parts[-2]:
                        return SEPARATOR_DOT.join(qn_parts[:-1])

    return None
