"""Lua's share of the call pass: the caller name a Lua function's body calls
are attributed under, which the definition pass derives from the function's
table field, its assignment target or a generated name.

`CallProcessor` owns what every language shares and names a Lua caller
through `caller_func_name`."""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from . import utils as lua_utils


def caller_func_name(func_node: Node) -> str | None:
    field = lua_utils.field_function_path(func_node)
    if field is not None:
        return field[0]
    # A field with no name is anonymous in the definition pass too;
    # naming it after the assignment here would credit its calls to a
    # node that does not exist (#1750).
    if not lua_utils.is_field_value(func_node) and (
        name := lua_utils.extract_assigned_name(
            func_node, accepted_var_types=cs.LUA_NAMING_ASSIGNMENT_TARGETS
        )
    ):
        return name
    # Nameless, but a node the program reaches: the value of
    # `t["lume.clamp"] =` or `t[k] =`, or a function the module returns
    # (`return function() ... end`, `return { function() ... end }`). Its
    # body's calls were dropped with the name (issue #2578); they belong
    # to the node the definition pass registered under the generated name.
    if lua_utils.bracket_assignment_target(
        func_node
    ) is not None or lua_utils.is_module_return_value(func_node):
        return lua_utils.anonymous_function_name(func_node)
    return None
