from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ...language_spec import LANGUAGE_FQN_SPECS
from ...utils.fqn_resolver import resolve_fqn_from_ast
from ..rs import utils as rs_utils
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from pathlib import Path

    from ...language_spec import LanguageSpec
    from ...types_defs import ASTNode


class RustHandler(BaseLanguageHandler):
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
            fqn_config := LANGUAGE_FQN_SPECS.get(cs.SupportedLanguage.RUST)
        ) and file_path:
            if func_qn := resolve_fqn_from_ast(
                node, file_path, repo_path, project_name, fqn_config
            ):
                return func_qn

        if path_parts := rs_utils.build_module_path(node):
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def should_process_as_impl_block(self, node: ASTNode) -> bool:
        return node.type == cs.TS_IMPL_ITEM

    def extract_impl_target(self, node: ASTNode) -> str | None:
        return rs_utils.extract_impl_target(node)
