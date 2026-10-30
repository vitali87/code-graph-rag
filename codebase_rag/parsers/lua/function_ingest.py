"""Lua's share of the function pass: the name a nameless Lua function value
takes from the table field it is built into, or from the assignment that
binds it.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and names
a nameless Lua function through the `LuaFunctionIngest` it builds; it holds
no Lua state."""

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFunctionIngest
from . import utils as lua_utils


def function_name(func_node: Node) -> tuple[str | None, str | None]:
    """(name, display name) a nameless Lua function value is registered
    under, else Nones."""
    # A function expression that is the VALUE of a table-constructor field
    # (`s = { set = function() end }`) is named by its key, under the table
    # it is built into: qn `s.set`, name `set`. Before, the enclosing
    # assignment's left side named it, so every function in the table was
    # `s` and only an `@line` suffix told them apart (issue #1631).
    if func_node.type != cs.TS_LUA_FUNCTION_DEFINITION:
        return None, None
    func_name, display_name = _field_function_name(func_node)
    # A field value whose field has no name is anonymous; only a
    # function that is NOT a field value may take the assignment's
    # name (#1750).
    if not func_name and not lua_utils.is_field_value(func_node):
        func_name = _assignment_function_name(func_node)
    return func_name, display_name


def _assignment_function_name(func_node: Node) -> str | None:
    return lua_utils.extract_assigned_name(
        func_node,
        accepted_var_types=cs.LUA_NAMING_ASSIGNMENT_TARGETS,
    )


def _field_function_name(func_node: Node) -> tuple[str | None, str | None]:
    """(`table.key` path, `key`) for a table field's function value, else Nones.

    The walk lives in `lua_utils.field_function_path`, shared with the
    call pass so both name the node identically (#1631).
    """
    found = lua_utils.field_function_path(func_node)
    return found if found is not None else (None, None)


class LuaFunctionIngest(LanguageFunctionIngest):
    """Names a Lua function value that has no name of its own.

    Built and owned by `FunctionIngest`."""

    __slots__ = ()

    def nameless_function_name(self, func_node: Node) -> tuple[str | None, str | None]:
        return function_name(func_node)
