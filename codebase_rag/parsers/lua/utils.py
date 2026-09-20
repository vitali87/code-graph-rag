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


def field_key_name(field: Node) -> str | None:
    """The key a table-constructor `field` binds: `f` in `f = ...`, `set` in
    `["set"] = ...`. None for a positional entry or a computed key.

    tree-sitter-lua exposes `k = v` and `[k] = v` alike as `name: identifier`;
    the opening bracket is what tells a computed key from a literal one
    (#1631 review), so a bracketed identifier is computed and names nothing.
    """
    key = field.child_by_field_name(cs.FIELD_NAME)
    if key is None:
        return None
    bracketed = bool(field.children) and field.children[0].type == cs.LUA_OPEN_BRACKET
    if key.type == cs.TS_LUA_IDENTIFIER:
        return None if bracketed else safe_decode_text(key)
    if key.type in cs.LUA_STRING_TYPES:
        content = next(
            (c for c in key.named_children if c.type == cs.TS_LUA_STRING_CONTENT),
            None,
        )
        return safe_decode_text(content) if content is not None else None
    return None


def field_function_path(func_node: Node) -> tuple[str, str] | None:
    """(`table.key` path, `key`) for a function that is a table field's value.

    Shared by the definition pass, which registers the node under the path,
    and the call pass, which must recover the same name or the body's calls
    are skipped or credited to the enclosing function (#1631 review). Nested
    constructors chain their keys (`M = { sub = { f = ... } }` gives
    `M.sub.f`); the outermost table takes the name its statement assigns it,
    through `extract_assigned_name`. A constructor with no assignment (a
    returned or passed table) names the function by its keys alone.

    None when the function is not a field value, when its key is positional
    or computed, or when any enclosing constructor sits in a positional or
    computed field: `{ { run = function() end } }` has no field `run` on the
    outer list, and inventing `list.run` brings back the `@line` collisions
    this exists to remove. The caller then falls back to the assignment form.
    """
    field = _field_valued_by(func_node)
    if field is None:
        return None
    key = field_key_name(field)
    if not key:
        return None
    parts = [key]
    table = field.parent
    while table is not None and table.type == cs.TS_LUA_TABLE_CONSTRUCTOR:
        enclosing = table.parent
        if enclosing is None or enclosing.type != cs.TS_LUA_FIELD:
            break
        outer_key = field_key_name(enclosing)
        if not outer_key:
            return None
        parts.insert(0, outer_key)
        table = enclosing.parent
    # The outermost constructor takes an owner only when it IS the value the
    # statement assigns: a direct child of the assignment's expression list.
    # `extract_assigned_name` accepts any descendant of a value, so a table
    # passed as an ARGUMENT (`local r = register({ f = ... })`) would take
    # `r`, which receives `register`'s return value and not the table, and
    # the function would carry a false identity `r.f` (#1750 review).
    if table is not None and _is_assigned_value(table):
        owner = extract_assigned_name(
            table, accepted_var_types=(cs.TS_DOT_INDEX_EXPRESSION, cs.TS_IDENTIFIER)
        )
        if owner:
            parts.insert(0, owner)
    return cs.SEPARATOR_DOT.join(parts), key


def _is_assigned_value(node: Node) -> bool:
    values = node.parent
    return (
        values is not None
        and values.type == cs.TS_LUA_EXPRESSION_LIST
        and values.parent is not None
        and values.parent.type == cs.TS_LUA_ASSIGNMENT_STATEMENT
    )


def is_field_value(func_node: Node) -> bool:
    """True when `func_node` is the value of a table-constructor field.

    The tri-state the callers need: `field_function_path` says None both for
    a function that is not a field value and for one whose field has no name
    (a computed `[k]` key, a positional entry, a nesting under either). Only
    the first may fall back to the enclosing assignment's name; the second
    is anonymous, and falling back named `{ [k] = function() end }` after the
    table it sits in (#1750 review).
    """
    return _field_valued_by(func_node) is not None


def _field_valued_by(func_node: Node) -> Node | None:
    """The table field whose value is `func_node`, seen through parentheses.

    `f = (function() end)` parses as field > parenthesized_expression >
    function_definition, so the field's value is the parentheses and not
    the function; without unwrapping, the function was not a field value
    and fell back to the assignment's name (CodeRabbit, #1750).
    """
    value: Node = func_node
    parent = value.parent
    while parent is not None and parent.type == cs.TS_PARENTHESIZED_EXPRESSION:
        value, parent = parent, parent.parent
    if (
        parent is None
        or parent.type != cs.TS_LUA_FIELD
        or parent.child_by_field_name(cs.FIELD_VALUE) != value
    ):
        return None
    return parent
