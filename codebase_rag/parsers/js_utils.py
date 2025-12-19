from tree_sitter import Node

from ..constants import SEPARATOR_DOT
from .utils import safe_decode_text


def extract_js_method_call(member_expr_node: Node) -> str | None:
    """Extract method call text from JavaScript member expression like Storage.getInstance.

    Args:
        member_expr_node: The member_expression AST node.

    Returns:
        The method call string (e.g., "Storage.getInstance"), or None if extraction fails.
    """
    try:
        object_node = member_expr_node.child_by_field_name("object")
        property_node = member_expr_node.child_by_field_name("property")

        if object_node and property_node:
            object_text = object_node.text
            property_text = property_node.text

            if object_text and property_text:
                object_name = safe_decode_text(object_node)
                property_name = safe_decode_text(property_node)
                return f"{object_name}.{property_name}"
    except Exception:
        return None

    return None


def find_js_method_in_class_body(
    class_body_node: Node, method_name: str
) -> Node | None:
    """Find a method by name within a JavaScript class body.

    Args:
        class_body_node: The class body AST node.
        method_name: Name of the method to find.

    Returns:
        The method_definition node, or None if not found.
    """
    for child in class_body_node.children:
        if child.type == "method_definition":
            name_node = child.child_by_field_name("name")
            if name_node and name_node.text:
                found_name = safe_decode_text(name_node)
                if found_name == method_name:
                    return child

    return None


def find_js_method_in_ast(
    root_node: Node, class_name: str, method_name: str
) -> Node | None:
    """Find a specific method within a JavaScript/TypeScript class in the AST.

    Args:
        root_node: The root AST node to search from.
        class_name: Name of the class containing the method.
        method_name: Name of the method to find.

    Returns:
        The method AST node, or None if not found.
    """
    stack: list[Node] = [root_node]

    while stack:
        current = stack.pop()

        if current.type == "class_declaration":
            name_node = current.child_by_field_name("name")
            if name_node and name_node.text:
                found_class_name = safe_decode_text(name_node)
                if found_class_name == class_name:
                    body_node = current.child_by_field_name("body")
                    if body_node:
                        return find_js_method_in_class_body(body_node, method_name)

        stack.extend(reversed(current.children))

    return None


def find_js_return_statements(node: Node, return_nodes: list[Node]) -> None:
    """Find all return statements in a JavaScript function.

    Uses iterative stack-based traversal to prevent RecursionError
    for deeply nested code.

    Args:
        node: The AST node to search in.
        return_nodes: List to accumulate found return statements.
    """
    stack: list[Node] = [node]

    while stack:
        current = stack.pop()

        if current.type == "return_statement":
            return_nodes.append(current)

        stack.extend(reversed(current.children))


def extract_js_constructor_name(new_expr_node: Node) -> str | None:
    """Extract constructor name from a 'new' expression.

    Args:
        new_expr_node: The new_expression AST node.

    Returns:
        The constructor class name, or None if not found.
    """
    if new_expr_node.type != "new_expression":
        return None

    constructor_node = new_expr_node.child_by_field_name("constructor")
    if constructor_node and constructor_node.type == "identifier":
        constructor_text = constructor_node.text
        if constructor_text:
            return safe_decode_text(constructor_node)

    return None


def analyze_js_return_expression(expr_node: Node, method_qn: str) -> str | None:
    """Analyze a JavaScript return expression to infer its type.

    Handles common patterns:
    - return new Storage() -> Storage
    - return this -> class type
    - return Storage.instance -> class type
    - return this.instance -> class type

    Args:
        expr_node: The return expression AST node.
        method_qn: Qualified name of the method containing the return.

    Returns:
        The inferred type name, or None if inference fails.
    """
    if expr_node.type == "new_expression":
        class_name = extract_js_constructor_name(expr_node)
        if class_name:
            qn_parts = method_qn.split(SEPARATOR_DOT)
            if len(qn_parts) >= 2:
                return SEPARATOR_DOT.join(qn_parts[:-1])
            return class_name

    elif expr_node.type == "this":
        qn_parts = method_qn.split(SEPARATOR_DOT)
        if len(qn_parts) >= 2:
            return SEPARATOR_DOT.join(qn_parts[:-1])

    elif expr_node.type == "member_expression":
        object_node = expr_node.child_by_field_name("object")
        if object_node:
            if object_node.type == "this":
                qn_parts = method_qn.split(SEPARATOR_DOT)
                if len(qn_parts) >= 2:
                    return SEPARATOR_DOT.join(qn_parts[:-1])
            elif object_node.type == "identifier":
                object_text = object_node.text
                if object_text:
                    object_name = safe_decode_text(object_node)
                    qn_parts = method_qn.split(SEPARATOR_DOT)
                    if len(qn_parts) >= 2 and object_name == qn_parts[-2]:
                        return SEPARATOR_DOT.join(qn_parts[:-1])

    return None
