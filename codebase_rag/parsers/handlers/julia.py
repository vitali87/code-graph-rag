from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..julia import utils as julia_utils
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode


class JuliaHandler(BaseLanguageHandler):
    __slots__ = ()

    def extract_function_name(self, node: ASTNode) -> str | None:
        # No `name` field on any Julia definition node: explicit functions
        # and macros carry the call-shaped head in a positional `signature`,
        # the concise method `f(x) = ...` is an `assignment`, and an arrow
        # function takes its name from the enclosing assignment.
        if node.type == cs.TS_JULIA_ARROW_FUNCTION_EXPRESSION:
            return julia_utils.julia_arrow_assigned_name(node)
        return julia_utils.julia_function_head_name(node)
