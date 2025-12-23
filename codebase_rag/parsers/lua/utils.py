from tree_sitter import Node

from ..utils import contains_node, safe_decode_text


def extract_assigned_name(
    target_node: Node, accepted_var_types: tuple[str, ...] = ("identifier",)
) -> str | None:
    current = target_node.parent
    while current and current.type != "assignment_statement":
        current = current.parent

    if not current:
        return None

    expression_list = next(
        (child for child in current.children if child.type == "expression_list"),
        None,
    )
    if not expression_list:
        return None

    values = []
    values.extend(
        expression_list.child(i)
        for i in range(expression_list.child_count)
        if expression_list.field_name_for_child(i) == "value"
    )
    target_index = next(
        (
            idx
            for idx, value in enumerate(values)
            if value == target_node or contains_node(value, target_node)
        ),
        -1,
    )
    if target_index == -1:
        return None

    variable_list = next(
        (child for child in current.children if child.type == "variable_list"),
        None,
    )
    if not variable_list:
        return None

    names = []
    names.extend(
        variable_list.child(i)
        for i in range(variable_list.child_count)
        if variable_list.field_name_for_child(i) == "name"
    )
    if target_index < len(names):
        var_child = names[target_index]
        if var_child.type in accepted_var_types:
            return safe_decode_text(var_child)

    return None


def find_ancestor_statement(node: Node) -> Node | None:
    stmt = node.parent
    while stmt and not (
        stmt.type.endswith("statement")
        or stmt.type in {"assignment_statement", "local_statement"}
    ):
        stmt = stmt.parent
    return stmt


def extract_pcall_second_identifier(call_node: Node) -> str | None:
    stmt = find_ancestor_statement(call_node)
    if not stmt:
        return None

    variable_list = next(
        (child for child in stmt.children if child.type == "variable_list"),
        None,
    )
    if not variable_list:
        return None

    names = []
    for i in range(variable_list.child_count):
        if variable_list.field_name_for_child(i) == "name":
            name_node = variable_list.child(i)
            if name_node and name_node.type == "identifier":
                if decoded := safe_decode_text(name_node):
                    names.append(decoded)

    return names[1] if len(names) >= 2 else None
