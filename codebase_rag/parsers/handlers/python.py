from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..utils import safe_decode_text
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode


class PythonHandler(BaseLanguageHandler):
    def extract_decorators(self, node: ASTNode) -> list[str]:
        if not node.parent or node.parent.type != cs.TS_PY_DECORATED_DEFINITION:
            return []
        return [
            decorator_name
            for child in node.parent.children
            if child.type == cs.TS_PY_DECORATOR
            if (decorator_name := self._get_decorator_name(child))
        ]

    def _get_decorator_name(self, decorator_node: ASTNode) -> str | None:
        for child in decorator_node.children:
            if child.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
                return safe_decode_text(child)
            if child.type == cs.TS_PY_CALL:
                if func_node := child.child_by_field_name(cs.FIELD_FUNCTION):
                    if func_node.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
                        return safe_decode_text(func_node)
        return None
