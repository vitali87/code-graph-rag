from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as lg
from ...types_defs import (
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    SimpleNameLookup,
)
from ..import_processor import ImportProcessor
from .ast_analyzer import PythonAstAnalyzerMixin
from .expression_analyzer import PythonExpressionAnalyzerMixin
from .variable_analyzer import PythonVariableAnalyzerMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..factory import ASTCacheProtocol
    from ..js_ts import JsTypeInferenceEngine


class PythonTypeInferenceEngine(
    PythonExpressionAnalyzerMixin,
    PythonAstAnalyzerMixin,
    PythonVariableAnalyzerMixin,
):
    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: ASTCacheProtocol,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        js_type_inference_getter: Callable[[], JsTypeInferenceEngine],
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance = class_inheritance
        self.simple_name_lookup = simple_name_lookup
        self._js_type_inference_getter = js_type_inference_getter

        self._method_return_type_cache: dict[str, str | None] = {}
        self._type_inference_in_progress: set[str] = set()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        try:
            self._infer_parameter_types(caller_node, local_var_types, module_qn)
            # (H) Single-pass traversal avoids O(5*N) multiple traversals for type inference.
            self._traverse_single_pass(caller_node, local_var_types, module_qn)

        except Exception as e:
            logger.debug(lg.PY_BUILD_VAR_MAP_FAILED.format(error=e))

        return local_var_types
