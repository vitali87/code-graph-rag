from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..java import utils as java_utils
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode


class JavaHandler(BaseLanguageHandler):
    def build_method_qualified_name(
        self,
        class_qn: str,
        method_name: str,
        method_node: ASTNode,
    ) -> str:
        if (method_info := java_utils.extract_method_info(method_node)) and method_info[
            cs.FIELD_PARAMETERS
        ]:
            param_sig = cs.SEPARATOR_COMMA_SPACE.join(method_info[cs.FIELD_PARAMETERS])
            return f"{class_qn}{cs.SEPARATOR_DOT}{method_name}({param_sig})"
        return f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
