from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..lua import utils as lua_utils
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode


class LuaHandler(BaseLanguageHandler):
    def extract_function_name(self, node: ASTNode) -> str | None:
        if (name_node := node.child_by_field_name(cs.TS_FIELD_NAME)) and name_node.text:
            from ..utils import safe_decode_text

            return safe_decode_text(name_node)

        if node.type == cs.TS_LUA_FUNCTION_DEFINITION:
            return lua_utils.extract_assigned_name(
                node, accepted_var_types=(cs.TS_DOT_INDEX_EXPRESSION, cs.TS_IDENTIFIER)
            )

        return None
