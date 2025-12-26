from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs
from ..types_defs import (
    ASTNode,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    SimpleNameLookup,
)
from .import_processor import ImportProcessor
from .java import JavaTypeInferenceEngine
from .js_ts import JsTypeInferenceEngine
from .lua import LuaTypeInferenceEngine
from .py import PythonTypeInferenceEngine, resolve_class_name

if TYPE_CHECKING:
    from .factory import ASTCacheProtocol


class TypeInferenceEngine:
    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: "ASTCacheProtocol",
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
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

        self._java_type_inference: JavaTypeInferenceEngine | None = None
        self._lua_type_inference: LuaTypeInferenceEngine | None = None
        self._js_type_inference: JsTypeInferenceEngine | None = None
        self._python_type_inference: PythonTypeInferenceEngine | None = None

    @property
    def java_type_inference(self) -> JavaTypeInferenceEngine:
        if self._java_type_inference is None:
            self._java_type_inference = JavaTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                repo_path=self.repo_path,
                project_name=self.project_name,
                ast_cache=self.ast_cache,
                queries=self.queries,
                module_qn_to_file_path=self.module_qn_to_file_path,
                class_inheritance=self.class_inheritance,
                simple_name_lookup=self.simple_name_lookup,
            )
        return self._java_type_inference

    @property
    def lua_type_inference(self) -> LuaTypeInferenceEngine:
        if self._lua_type_inference is None:
            self._lua_type_inference = LuaTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                project_name=self.project_name,
            )
        return self._lua_type_inference

    @property
    def js_type_inference(self) -> JsTypeInferenceEngine:
        if self._js_type_inference is None:
            self._js_type_inference = JsTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                project_name=self.project_name,
                find_method_ast_node_func=self.python_type_inference._find_method_ast_node,
            )
        return self._js_type_inference

    @property
    def python_type_inference(self) -> PythonTypeInferenceEngine:
        if self._python_type_inference is None:
            self._python_type_inference = PythonTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                repo_path=self.repo_path,
                project_name=self.project_name,
                ast_cache=self.ast_cache,
                queries=self.queries,
                module_qn_to_file_path=self.module_qn_to_file_path,
                class_inheritance=self.class_inheritance,
                simple_name_lookup=self.simple_name_lookup,
                js_type_inference_getter=lambda: self.js_type_inference,
            )
        return self._python_type_inference

    def build_local_variable_type_map(
        self, caller_node: ASTNode, module_qn: str, language: cs.SupportedLanguage
    ) -> dict[str, str]:
        match language:
            case cs.SupportedLanguage.PYTHON:
                return self.python_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS:
                return self.js_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.JAVA:
                return self.java_type_inference.build_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.LUA:
                return self.lua_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case _:
                return {}

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _build_java_variable_type_map(
        self, caller_node: ASTNode, module_qn: str
    ) -> dict[str, str]:
        return self.java_type_inference.build_variable_type_map(caller_node, module_qn)
