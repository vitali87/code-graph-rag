from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import (
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    SimpleNameLookup,
)
from ..definition_facts import DefinitionFacts
from .ast_analyzer import PythonAstAnalyzer
from .expression_analyzer import PythonExpressionAnalyzer
from .variable_analyzer import PythonVariableAnalyzer
from .with_analyzer import PythonWithBinding

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ...types_defs import ASTCacheProtocol
    from ..import_processor import ImportProcessor
    from ..js_ts import JsTypeInferenceEngine


class PythonTypeInferenceEngine:
    __slots__ = (
        "repo_path",
        "project_name",
        "_js_type_inference_getter",
        "_method_return_type_cache",
        "_type_inference_in_progress",
        "_available_classes_cache",
        "_return_stmt_cache",
        "_self_assignment_cache",
        "_class_member_type_cache",
        "ast_analyzer",
        "expression_analyzer",
        "variable_analyzer",
        "with_binding",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: ASTCacheProtocol,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        facts: DefinitionFacts,
        simple_name_lookup: SimpleNameLookup,
        js_type_inference_getter: Callable[[], JsTypeInferenceEngine],
    ):
        self.repo_path = repo_path
        self.project_name = project_name
        self._js_type_inference_getter = js_type_inference_getter

        self._method_return_type_cache: dict[str, str | None] = {}
        self._type_inference_in_progress: set[str] = set()
        self._available_classes_cache: dict[str, list[str]] = {}
        # Keyed by the Node itself, never id(node): Node hashes by its tree-sitter
        # identity, while a freed wrapper's id() is reused by unrelated nodes,
        # producing stale hits that vary with memory layout (nondeterministic graphs,
        # caught by the determinism test). Keys hold the node, so entries never collide.
        self._return_stmt_cache: dict[Node, list[Node]] = {}
        self._self_assignment_cache: dict[tuple[Node, str], dict[str, str] | None] = {}
        self._class_member_type_cache: dict[str, dict[str, str]] = {}

        self.ast_analyzer = PythonAstAnalyzer(
            import_processor=import_processor,
            function_registry=function_registry,
            module_qn_to_file_path=module_qn_to_file_path,
            ast_cache=ast_cache,
            queries=queries,
            return_stmt_cache=self._return_stmt_cache,
        )
        self.expression_analyzer = PythonExpressionAnalyzer(
            import_processor=import_processor,
            function_registry=function_registry,
            simple_name_lookup=simple_name_lookup,
            module_qn_to_file_path=module_qn_to_file_path,
            ast_cache=ast_cache,
            method_return_type_cache=self._method_return_type_cache,
            self_assignment_cache=self._self_assignment_cache,
            ast_analyzer=self.ast_analyzer,
        )
        self.variable_analyzer = PythonVariableAnalyzer(
            import_processor=import_processor,
            function_registry=function_registry,
            queries=queries,
            facts=facts,
            available_classes_cache=self._available_classes_cache,
            class_member_type_cache=self._class_member_type_cache,
            ast_analyzer=self.ast_analyzer,
            expression_analyzer=self.expression_analyzer,
        )
        self.with_binding = PythonWithBinding(
            import_processor=import_processor,
            function_registry=function_registry,
            ast_analyzer=self.ast_analyzer,
            expression_analyzer=self.expression_analyzer,
            variable_analyzer=self.variable_analyzer,
        )
        # The analyzers recurse into one another. A body's assignment passes
        # (the AST analyzer's) type each value through the expression analyzer,
        # each `with` target through the with binding and each loop variable
        # through the variable analyzer. Typing an expression reads a callee's
        # returns, and a `return w` reads the callee's own type map, which the
        # variable analyzer builds with those same passes; an inline operator
        # receiver is typed by the variable analyzer's alias rules. Each is
        # built with the ones it reads that exist by then, and the AST and
        # expression analyzers are handed the rest last.
        self.expression_analyzer.bind_variable_analyzer(self.variable_analyzer)
        self.ast_analyzer.bind_analyzers(
            self.expression_analyzer, self.variable_analyzer, self.with_binding
        )

    def clear_pass_caches(self) -> None:
        """Drop the per-pass memos: on a long-lived updater an edited
        function's old return type would re-resolve its dependents, and the
        Node-keyed entries would pin every re-parsed tree (#2927)."""
        self._available_classes_cache.clear()
        self._return_stmt_cache.clear()
        self._method_return_type_cache.clear()
        self._self_assignment_cache.clear()
        self._class_member_type_cache.clear()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str, class_context: str | None = None
    ) -> dict[str, str]:
        return self.variable_analyzer.build_local_variable_type_map(
            caller_node, module_qn, class_context
        )
