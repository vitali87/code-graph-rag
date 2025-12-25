from tree_sitter import Node

from ... import constants as cs
from ..utils import contains_node, safe_decode_text


def extract_assigned_name(
    target_node: Node, accepted_var_types: tuple[str, ...] = cs.LUA_DEFAULT_VAR_TYPES
) -> str | None:
    current = target_node.parent
    while current and current.type != cs.TS_LUA_ASSIGNMENT_STATEMENT:
        current = current.parent

    if not current:
        return None

    expression_list = next(
        (
            child
            for child in current.children
            if child.type == cs.TS_LUA_EXPRESSION_LIST
        ),
        None,
    )
    if not expression_list:
        return None

    values = []
    values.extend(
        expression_list.child(i)
        for i in range(expression_list.child_count)
        if expression_list.field_name_for_child(i) == cs.FIELD_VALUE
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
        (child for child in current.children if child.type == cs.TS_LUA_VARIABLE_LIST),
        None,
    )
    if not variable_list:
        return None

    names = []
    names.extend(
        variable_list.child(i)
        for i in range(variable_list.child_count)
        if variable_list.field_name_for_child(i) == cs.FIELD_NAME
    )
    if target_index < len(names):
        var_child = names[target_index]
        if var_child.type in accepted_var_types:
            return safe_decode_text(var_child)

    return None


def find_ancestor_statement(node: Node) -> Node | None:
    stmt = node.parent
    while stmt and not (
        stmt.type.endswith(cs.LUA_STATEMENT_SUFFIX)
        or stmt.type in {cs.TS_LUA_ASSIGNMENT_STATEMENT, cs.TS_LUA_LOCAL_STATEMENT}
    ):
        stmt = stmt.parent
    return stmt


def extract_pcall_second_identifier(call_node: Node) -> str | None:
    stmt = find_ancestor_statement(call_node)
    if not stmt:
        return None

    variable_list = next(
        (child for child in stmt.children if child.type == cs.TS_LUA_VARIABLE_LIST),
        None,
    )
    if not variable_list:
        return None

    names = []
    for i in range(variable_list.child_count):
        if variable_list.field_name_for_child(i) == cs.FIELD_NAME:
            name_node = variable_list.child(i)
            if name_node and name_node.type == cs.TS_LUA_IDENTIFIER:
                if decoded := safe_decode_text(name_node):
                    names.append(decoded)

    return names[1] if len(names) >= 2 else None
