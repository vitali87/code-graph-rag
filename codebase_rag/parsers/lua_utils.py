"""Utilities for processing Lua code with tree-sitter."""

from tree_sitter import Node

from .utils import contains_node, safe_decode_text


def extract_lua_assigned_name(
    target_node: Node, accepted_var_types: tuple[str, ...] = ("identifier",)
) -> str | None:
    """Extract the variable name assigned to a target node in a Lua assignment.

    This function handles patterns like:
    - local a = func()
    - local a, b, c = func1(), func2(), func3()
    - Calculator.divide = function() end

    Args:
        target_node: The node (typically a function or call) whose assigned name to find.
        accepted_var_types: Tuple of acceptable variable node types to extract.
                           Defaults to ("identifier",).

    Returns:
        The name of the variable assigned to the target node, or None if not found.
    """
    # Look for parent assignment_statement
    current = target_node.parent
    while current and current.type != "assignment_statement":
        current = current.parent

    if not current:
        return None

    # Find the expression_list containing our target node
    expression_list = None
    for child in current.children:
        if child.type == "expression_list":
            expression_list = child
            break

    if not expression_list:
        return None

    # Get all value fields from expression_list
    values = []
    for i in range(expression_list.child_count):
        if expression_list.field_name_for_child(i) == "value":
            values.append(expression_list.child(i))

    # Find which value contains our target node
    target_index = -1
    for idx, value in enumerate(values):
        if value == target_node or contains_node(value, target_node):
            target_index = idx
            break

    if target_index == -1:
        return None

    # Find the variable_list and get the corresponding name field
    variable_list = None
    for child in current.children:
        if child.type == "variable_list":
            variable_list = child
            break

    if not variable_list:
        return None

    # Get all name fields from variable_list
    names = []
    for i in range(variable_list.child_count):
        if variable_list.field_name_for_child(i) == "name":
            names.append(variable_list.child(i))

    # Get the corresponding variable name
    if target_index < len(names):
        var_child = names[target_index]
        if var_child.type in accepted_var_types:
            return safe_decode_text(var_child)

    return None


def find_lua_ancestor_statement(node: Node) -> Node | None:
    """Find the nearest statement-like ancestor of a node.

    Args:
        node: The node to start from.

    Returns:
        The nearest ancestor that is a statement node, or None if not found.
    """
    stmt = node.parent
    while stmt and not (
        stmt.type.endswith("statement")
        or stmt.type in {"assignment_statement", "local_statement"}
    ):
        stmt = stmt.parent
    return stmt


def extract_lua_pcall_second_identifier(call_node: Node) -> str | None:
    """Extract the second identifier from a pcall assignment pattern.

    In patterns like: local ok, json = pcall(require, 'json')
    We want to extract 'json' (the second identifier).

    Args:
        call_node: The pcall call node.

    Returns:
        The second identifier name if found, None otherwise.
    """
    stmt = find_lua_ancestor_statement(call_node)
    if not stmt:
        return None

    # Look for variable_list node which contains the identifiers
    variable_list = None
    for child in stmt.children:
        if child.type == "variable_list":
            variable_list = child
            break

    if not variable_list:
        return None

    # Get all name fields from variable_list
    names = []
    for i in range(variable_list.child_count):
        if variable_list.field_name_for_child(i) == "name":
            name_node = variable_list.child(i)
            if name_node.type == "identifier":
                decoded = safe_decode_text(name_node)
                if decoded:
                    names.append(decoded)

    # Return the second identifier if it exists (first is typically 'ok' or error status)
    if len(names) >= 2:
        return names[1]

    return None
