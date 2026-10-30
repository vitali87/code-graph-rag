"""Lua's share of the call pass: the caller name a Lua function's body calls
are attributed under, which the definition pass derives from the function's
table field, its assignment target or a generated name.

`CallProcessor` owns what every language shares and hands Lua's part to the
`LuaCallResolution` it builds."""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..language_calls import LanguageCalls
from . import utils as lua_utils


class LuaCallResolution(LanguageCalls):
    """Names a Lua caller as the definition pass names it.

    Built and owned by `CallProcessor`."""

    __slots__ = ()

    def caller_name(self, func_node: Node, module_qn: str) -> str | None:
        if name := super().caller_name(func_node, module_qn):
            return name
        if func_node.type != cs.TS_LUA_FUNCTION_DEFINITION:
            return None
        # A function expression bound to a variable or table field
        # (`local f = function()`, `M.f = function()`) has no name field;
        # the definition pass names it after its assignment target, so
        # recover the same name here or the whole body is skipped. A
        # table-constructor field value is named by its key path first
        # (issue #1631), through the SAME helper the definition pass
        # uses: a returned or passed table has no assignment target at
        # all, and without this the body's calls were dropped or
        # credited to the enclosing function (#1631).
        return caller_func_name(func_node) or None


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
