from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..utils import safe_decode_text
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode


class PythonHandler(BaseLanguageHandler):
    __slots__ = ()

    def extract_decorators(self, node: ASTNode) -> list[str]:
        if not node.parent or node.parent.type != cs.TS_PY_DECORATED_DEFINITION:
            return []
        return [
            decorator_text
            for child in node.parent.children
            if child.type == cs.TS_PY_DECORATOR
            if (decorator_text := safe_decode_text(child))
        ]
