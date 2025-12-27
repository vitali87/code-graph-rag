from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ...language_spec import LANGUAGE_FQN_SPECS
from ...utils.fqn_resolver import resolve_fqn_from_ast
from ..cpp import utils as cpp_utils
from ..utils import safe_decode_text
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from pathlib import Path

    from ...language_spec import LanguageSpec
    from ...types_defs import ASTNode


class CppHandler(BaseLanguageHandler):
    def extract_function_name(self, node: ASTNode) -> str | None:
        if func_name := cpp_utils.extract_function_name(node):
            return func_name

        if node.type == cs.TS_CPP_LAMBDA_EXPRESSION:
            return f"lambda_{node.start_point[0]}_{node.start_point[1]}"

        return None

    def build_function_qualified_name(
        self,
        node: ASTNode,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec | None,
        file_path: Path | None,
        repo_path: Path,
        project_name: str,
    ) -> str:
        if (
            fqn_config := LANGUAGE_FQN_SPECS.get(cs.SupportedLanguage.CPP)
        ) and file_path:
            if func_qn := resolve_fqn_from_ast(
                node, file_path, repo_path, project_name, fqn_config
            ):
                return func_qn

        return cpp_utils.build_qualified_name(node, module_qn, func_name)

    def is_function_exported(self, node: ASTNode) -> bool:
        return cpp_utils.is_exported(node)

    def extract_base_class_name(self, base_node: ASTNode) -> str | None:
        if base_node.type == cs.TS_TEMPLATE_TYPE:
            if (
                name_node := base_node.child_by_field_name(cs.TS_FIELD_NAME)
            ) and name_node.text:
                return safe_decode_text(name_node)

        return safe_decode_text(base_node) if base_node.text else None
