from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs
from ..types_defs import (
    ASTNode,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    SimpleNameLookup,
)
from .cpp import CppTypeInferenceEngine
from .go import GoTypeInferenceEngine
from .import_processor import ImportProcessor
from .java import JavaTypeInferenceEngine
from .js_ts import JsTypeInferenceEngine
from .lua import LuaTypeInferenceEngine
from .py import PythonTypeInferenceEngine, resolve_class_name

if TYPE_CHECKING:
    from .factory import ASTCacheProtocol


class TypeInferenceEngine:
    __slots__ = (
        "import_processor",
        "function_registry",
        "repo_path",
        "project_name",
        "ast_cache",
        "queries",
        "module_qn_to_file_path",
        "class_inheritance",
        "simple_name_lookup",
        "class_field_types",
        "method_return_types",
        "_java_type_inference",
        "_lua_type_inference",
        "_js_type_inference",
        "_python_type_inference",
        "_go_type_inference",
        "_cpp_type_inference",
    )

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
        class_field_types: dict[str, dict[str, str]] | None = None,
        method_return_types: dict[str, str] | None = None,
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
        # (H) Must preserve the shared dict reference: the factory passes the
        # (H) DefinitionProcessor's map, which is empty at construction and populated
        # (H) later during ingestion. `or {}` would swap an empty dict for a new one and
        # (H) silently lose every field type written afterward.
        self.class_field_types = (
            class_field_types if class_field_types is not None else {}
        )
        # (H) Shared reference (as with class_field_types): DefinitionProcessor's
        # (H) func_qn -> return-type map, populated during ingestion and read by the
        # (H) resolver's chained-call path.
        self.method_return_types = (
            method_return_types if method_return_types is not None else {}
        )

        self._java_type_inference: JavaTypeInferenceEngine | None = None
        self._lua_type_inference: LuaTypeInferenceEngine | None = None
        self._js_type_inference: JsTypeInferenceEngine | None = None
        self._python_type_inference: PythonTypeInferenceEngine | None = None
        self._go_type_inference: GoTypeInferenceEngine | None = None
        self._cpp_type_inference: CppTypeInferenceEngine | None = None

    @property
    def go_type_inference(self) -> GoTypeInferenceEngine:
        if self._go_type_inference is None:
            self._go_type_inference = GoTypeInferenceEngine()
        return self._go_type_inference

    @property
    def cpp_type_inference(self) -> CppTypeInferenceEngine:
        if self._cpp_type_inference is None:
            self._cpp_type_inference = CppTypeInferenceEngine()
        return self._cpp_type_inference

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
                queries=self.queries,
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
        self,
        caller_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None = None,
    ) -> dict[str, str]:
        local = self._build_local_variable_type_map(caller_node, module_qn, language)
        # (H) When the caller is a method, overlay its class's member-field types as a
        # (H) base so a bare `field_.method()` receiver resolves; parameters and locals
        # (H) with the same name shadow a field, so the local map wins on conflict.
        if class_context and (fields := self._collect_field_types(class_context)):
            local = {**fields, **local}
        if language == cs.SupportedLanguage.GO:
            self._enrich_go_call_locals(caller_node, module_qn, local)
        return local

    def _enrich_go_call_locals(
        self, caller_node: ASTNode, module_qn: str, var_types: dict[str, str]
    ) -> None:
        # (H) Type a Go local bound from a method call (`root := engine.trees.get(m)`)
        # (H) with the call's return type, so a later `root.addRoute()` resolves to the
        # (H) real type (node) instead of mis-resolving to the enclosing class's
        # (H) same-named method. Resolves the callee selector hop by hop: base local
        # (H) type, struct-field types for middle hops, then the final method's
        # (H) recorded return type. Only fills names not already typed.
        for name, segments in self.go_type_inference.collect_call_var_bindings(
            caller_node
        ):
            if name in var_types:
                continue
            if return_type := self._infer_go_call_return_type(
                segments, module_qn, var_types
            ):
                var_types[name] = return_type

    def _infer_go_call_return_type(
        self, segments: list[str], module_qn: str, var_types: dict[str, str]
    ) -> str | None:
        # (H) `['e','trees','get']`: base `e` -> Engine (a typed local), field `trees`
        # (H) -> its struct-field type, then method `get` -> its recorded return type.
        # (H) A plain function (`['f']`) has no receiver, so its return type is not in
        # (H) the method map and stays unresolved (rare in the receiver-dispatch gap).
        if len(segments) < 2:
            return None
        base_type = var_types.get(segments[0])
        if not base_type:
            return None
        class_qn = self._resolve_class_name(base_type, module_qn) or base_type
        for field in segments[1:-1]:
            field_type = self.class_field_types.get(class_qn, {}).get(field)
            if not field_type:
                return None
            class_qn = self._resolve_class_name(field_type, module_qn) or field_type
        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{segments[-1]}"
        return self.method_return_types.get(method_qn)

    def _collect_field_types(self, class_qn: str) -> dict[str, str]:
        # (H) Collect member-field types along the inheritance chain so a derived class
        # (H) method can resolve a field inherited from a base. Bases are visited first
        # (H) and the class's own fields applied last, so a derived field shadows a
        # (H) base field of the same name. Guards against inheritance cycles.
        fields: dict[str, str] = {}
        seen: set[str] = set()

        def collect(qn: str) -> None:
            if qn in seen:
                return
            seen.add(qn)
            for base in self.class_inheritance.get(qn, []):
                collect(base)
            if own := self.class_field_types.get(qn):
                fields.update(own)

        collect(class_qn)
        return fields

    def _build_local_variable_type_map(
        self, caller_node: ASTNode, module_qn: str, language: cs.SupportedLanguage
    ) -> dict[str, str]:
        match language:
            case cs.SupportedLanguage.PYTHON:
                return self.python_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case (
                cs.SupportedLanguage.JS
                | cs.SupportedLanguage.TS
                | cs.SupportedLanguage.TSX
            ):
                return self.js_type_inference.build_local_variable_type_map(
                    caller_node, module_qn, language
                )
            case cs.SupportedLanguage.JAVA:
                return self.java_type_inference.build_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.LUA:
                return self.lua_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.GO:
                return self.go_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.CPP:
                return self.cpp_type_inference.build_local_variable_type_map(
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
