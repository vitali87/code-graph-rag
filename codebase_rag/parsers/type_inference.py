from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs
from ..types_defs import (
    ASTNode,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    NodeType,
    SimpleNameLookup,
)
from .cpp import CppTypeInferenceEngine
from .csharp.type_inference import CSharpTypeInferenceEngine
from .csharp_frontend import CallSiteKey, CSharpCallSite
from .dart.type_inference import DartTypeInferenceEngine
from .go import GoTypeInferenceEngine
from .import_processor import ImportProcessor
from .java import JavaTypeInferenceEngine
from .js_ts import JsTypeInferenceEngine
from .lua import LuaTypeInferenceEngine
from .py import PythonTypeInferenceEngine, resolve_class_name
from .rs import RustTypeInferenceEngine

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
        "class_field_guard_inner",
        "method_return_types",
        "go_function_return_types",
        "csharp_partial_groups",
        "csharp_extension_methods",
        "csharp_call_sites",
        "csharp_external_sites",
        "csharp_local_functions",
        "csharp_generic_methods",
        "csharp_class_generic_arity",
        "csharp_method_return_types",
        "function_locations",
        "_java_type_inference",
        "_csharp_type_inference",
        "_lua_type_inference",
        "_js_type_inference",
        "_python_type_inference",
        "_go_type_inference",
        "_go_free_fn_index",
        "_go_free_fn_index_size",
        "_rust_type_inference",
        "_cpp_type_inference",
        "_dart_type_inference",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: "ASTCacheProtocol",
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
        class_field_types: dict[str, dict[str, str]] | None = None,
        class_field_guard_inner: dict[str, dict[str, str]] | None = None,
        method_return_types: dict[str, str] | None = None,
        go_function_return_types: dict[str, str] | None = None,
        csharp_partial_groups: dict[str, list[str]] | None = None,
        csharp_extension_methods: dict[str, list[tuple[str, str, str, int]]]
        | None = None,
        csharp_call_sites: dict[CallSiteKey, CSharpCallSite] | None = None,
        csharp_external_sites: set[CallSiteKey] | None = None,
        csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] | None = None,
        csharp_generic_methods: set[str] | None = None,
        csharp_class_generic_arity: dict[str, int] | None = None,
        csharp_method_return_types: dict[str, tuple[str, int]] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
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
        # Must preserve the shared dict reference: the factory passes the
        # DefinitionProcessor's map, which is empty at construction and populated
        # later during ingestion. `or {}` would swap an empty dict for a new one and
        # silently lose every field type written afterward.
        self.class_field_types = (
            class_field_types if class_field_types is not None else {}
        )
        # Shared reference (as with class_field_types): Rust guard-field inner types
        # (`Shared.state` -> State for a `Mutex<State>` field), applied only at a
        # guard-accessor hop so a direct wrapper call isn't mis-resolved to the inner.
        self.class_field_guard_inner = (
            class_field_guard_inner if class_field_guard_inner is not None else {}
        )
        # Shared reference (as with class_field_types): DefinitionProcessor's
        # func_qn -> return-type map, populated during ingestion and read by the
        # resolver's chained-call path.
        self.method_return_types = (
            method_return_types if method_return_types is not None else {}
        )
        # Shared reference (as with class_field_types): Go free-fn qn ->
        # FIRST return type, read only by the single-segment binding path.
        self.go_function_return_types = (
            go_function_return_types if go_function_return_types is not None else {}
        )
        self._go_free_fn_index: dict[tuple[str, str], str] = {}
        self._go_free_fn_index_size = -1
        # Shared reference (as with class_field_types): C# partial-class part
        # groups, populated during ingestion and read by the C# resolver to
        # span all parts of a split type.
        self.csharp_partial_groups = (
            csharp_partial_groups if csharp_partial_groups is not None else {}
        )
        # Shared reference (as with class_field_types): C# extension-method
        # index, populated during ingestion and read by the C# resolver's
        # receiver-binding fallback.
        self.csharp_extension_methods = (
            csharp_extension_methods if csharp_extension_methods is not None else {}
        )
        # Shared references (as with class_field_types): the Roslyn call-site
        # facts and the Pass-2 function-location registry, both populated
        # after construction and read by the C# resolver's semantic path.
        self.csharp_call_sites = (
            csharp_call_sites if csharp_call_sites is not None else {}
        )
        self.csharp_external_sites = (
            csharp_external_sites if csharp_external_sites is not None else set()
        )
        # Shared reference (as with class_field_types): C# local-function
        # host/arity index, populated during ingestion and read by the C#
        # resolver's bare-name path.
        self.csharp_local_functions = (
            csharp_local_functions if csharp_local_functions is not None else {}
        )
        # Shared reference (as with class_field_types): generic-method qn
        # set, populated during ingestion, read by C# bare-call dispatch.
        self.csharp_generic_methods = (
            csharp_generic_methods if csharp_generic_methods is not None else set()
        )
        self.csharp_class_generic_arity = (
            csharp_class_generic_arity if csharp_class_generic_arity is not None else {}
        )
        self.csharp_method_return_types = (
            csharp_method_return_types if csharp_method_return_types is not None else {}
        )
        self.function_locations = (
            function_locations if function_locations is not None else {}
        )

        self._java_type_inference: JavaTypeInferenceEngine | None = None
        self._csharp_type_inference: CSharpTypeInferenceEngine | None = None
        self._dart_type_inference: DartTypeInferenceEngine | None = None
        self._lua_type_inference: LuaTypeInferenceEngine | None = None
        self._js_type_inference: JsTypeInferenceEngine | None = None
        self._python_type_inference: PythonTypeInferenceEngine | None = None
        self._go_type_inference: GoTypeInferenceEngine | None = None
        self._rust_type_inference: RustTypeInferenceEngine | None = None
        self._cpp_type_inference: CppTypeInferenceEngine | None = None

    @property
    def go_type_inference(self) -> GoTypeInferenceEngine:
        if self._go_type_inference is None:
            self._go_type_inference = GoTypeInferenceEngine()
        return self._go_type_inference

    @property
    def rust_type_inference(self) -> RustTypeInferenceEngine:
        if self._rust_type_inference is None:
            self._rust_type_inference = RustTypeInferenceEngine()
        return self._rust_type_inference

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
    def csharp_type_inference(self) -> CSharpTypeInferenceEngine:
        if self._csharp_type_inference is None:
            self._csharp_type_inference = CSharpTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                repo_path=self.repo_path,
                project_name=self.project_name,
                ast_cache=self.ast_cache,
                queries=self.queries,
                module_qn_to_file_path=self.module_qn_to_file_path,
                class_inheritance=self.class_inheritance,
                simple_name_lookup=self.simple_name_lookup,
                class_field_types=self.class_field_types,
                csharp_partial_groups=self.csharp_partial_groups,
                csharp_extension_methods=self.csharp_extension_methods,
                csharp_call_sites=self.csharp_call_sites,
                csharp_external_sites=self.csharp_external_sites,
                csharp_local_functions=self.csharp_local_functions,
                csharp_generic_methods=self.csharp_generic_methods,
                csharp_class_generic_arity=self.csharp_class_generic_arity,
                csharp_method_return_types=self.csharp_method_return_types,
                method_return_types=self.method_return_types,
                function_locations=self.function_locations,
            )
        return self._csharp_type_inference

    @property
    def dart_type_inference(self) -> DartTypeInferenceEngine:
        if self._dart_type_inference is None:
            self._dart_type_inference = DartTypeInferenceEngine()
        return self._dart_type_inference

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
        # When the caller is a method, overlay its class's member-field types as a
        # base so a bare `field_.method()` receiver resolves; parameters and locals
        # with the same name shadow a field, so the local map wins on conflict.
        fields = self._collect_field_types(class_context) if class_context else {}
        if fields:
            local = {**fields, **local}
        if language == cs.SupportedLanguage.GO:
            self._enrich_go_call_locals(caller_node, module_qn, local)
        elif language == cs.SupportedLanguage.RUST:
            if class_context:
                # Rust member calls carry the explicit `self` receiver: type `self`
                # to the impl target (so `self.accept()` dispatches) and each
                # `self.<field>` to the field's type (so `self.shutdown.is_shutdown()`
                # hops through the field). setdefault: a same-named local wins.
                local.setdefault(cs.KEYWORD_SELF, class_context)
                for field, ftype in fields.items():
                    local.setdefault(
                        f"{cs.KEYWORD_SELF}{cs.SEPARATOR_DOT}{field}", ftype
                    )
            self._enrich_rust_call_locals(caller_node, module_qn, local)
        elif language == cs.SupportedLanguage.DART:
            self._enrich_dart_call_locals(caller_node, local)
        return local

    def _enrich_dart_call_locals(
        self, caller_node: ASTNode, var_types: dict[str, str]
    ) -> None:
        # A local bound from a class-qualified call (`var s =
        # Base.member(args)`) was heuristically typed as Base; when the
        # member is a REGISTERED method with a recorded return type, that
        # type wins (a named constructor or same-class factory keeps Base,
        # a `static String describe()` local becomes a String whose member
        # calls then drop as external). A registered member WITHOUT a
        # recorded return (void) untypes the local; an unregistered member
        # (external library factory) keeps the heuristic.
        bindings = self.dart_type_inference.collect_static_call_bindings(caller_node)
        for name, (base, member) in bindings.items():
            if var_types.get(name) != base:
                continue
            method_qn = self._dart_unique_class_member(base, member)
            if method_qn is None:
                continue
            recorded = self.method_return_types.get(method_qn)
            if recorded is None:
                var_types.pop(name, None)
            else:
                var_types[name] = recorded

    def _dart_unique_class_member(self, base: str, member: str) -> str | None:
        suffix = f"{cs.SEPARATOR_DOT}{base}{cs.SEPARATOR_DOT}{member}"
        matches = [
            qn
            for qn in self.function_registry.find_ending_with(member)
            if qn.endswith(suffix)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _enrich_go_call_locals(
        self, caller_node: ASTNode, module_qn: str, var_types: dict[str, str]
    ) -> None:
        # Type a Go local bound from a method call (`root := engine.trees.get(m)`)
        # with the call's return type, so a later `root.addRoute()` resolves to the
        # real type (node) instead of mis-resolving to the enclosing class's
        # same-named method. Resolves the callee selector hop by hop: base local
        # type, struct-field types for middle hops, then the final method's
        # recorded return type. Only fills names not already typed.
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
        # `['e','trees','get']`: base `e` -> Engine (a typed local), field `trees`
        # -> its struct-field type, then method `get` -> its recorded return type.
        # A plain function (`['f']`) types from the free-fn return map:
        # same-module first, then a same-package sibling file (Go package
        # scope spans the directory, viper's remote.go shape).
        if len(segments) == 1:
            return self._go_free_fn_return_type(segments[0], module_qn)
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

    def _go_free_fn_return_type(self, name: str, module_qn: str) -> str | None:
        # Same module (file) first; then the enclosing package's sibling
        # files (same parent dir), since Go free functions are
        # package-scoped, not file-scoped. The sibling lookup goes through
        # a lazily rebuilt (package, name) index: the shared map fills
        # DURING ingestion, so an init-time index would be empty, and the
        # size check rebuilds only when entries were added since.
        if hit := self.go_function_return_types.get(
            f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        ):
            return hit
        if cs.SEPARATOR_DOT not in module_qn:
            return None
        if len(self.go_function_return_types) != self._go_free_fn_index_size:
            self._go_free_fn_index = {}
            for qn, return_type in self.go_function_return_types.items():
                head, _, fn_name = qn.rpartition(cs.SEPARATOR_DOT)
                package, _, _file = head.rpartition(cs.SEPARATOR_DOT)
                if package:
                    self._go_free_fn_index.setdefault((package, fn_name), return_type)
            self._go_free_fn_index_size = len(self.go_function_return_types)
        package_prefix = module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return self._go_free_fn_index.get((package_prefix, name))

    def _enrich_rust_call_locals(
        self, caller_node: ASTNode, module_qn: str, var_types: dict[str, str]
    ) -> None:
        # Type a Rust local bound from an associated-function call
        # (`let cmd = Command::from_frame(f)?`) with the call's return type, so a
        # later `cmd.apply()` resolves to the real type instead of the ambiguous
        # name-only trie fallback. Only fills names not already typed.
        for name, segments in self.rust_type_inference.collect_call_var_bindings(
            caller_node
        ):
            if name in var_types:
                continue
            if return_type := self._infer_rust_call_return_type(
                segments, module_qn, var_types
            ):
                var_types[name] = return_type

    def _infer_rust_call_return_type(
        self, segments: list[str], module_qn: str, var_types: dict[str, str]
    ) -> str | None:
        # Walk a flattened chain to the type it yields:
        #   ['Command','from_frame']  -> base type Command, method from_frame
        #   ['self','shared','state','lock','unwrap'] -> base local self (Db),
        #     field shared (Arc<Shared>->Shared), field state (Mutex<State>, guard
        #     inner State), lock unwraps the guard -> State, unwrap identity -> State.
        # Each hop tries: guard-unwrap (a guard accessor right after a guard-
        # wrapped field) -> field-type -> method-return -> identity.
        if not segments:
            return None
        current_type = self._rust_chain_base_type(segments, module_qn, var_types)
        if current_type is None:
            return None
        # Inner type of the guard-wrapped field just hopped through, pending a guard
        # accessor to unwrap it (None otherwise).
        guard_inner: str | None = None
        for hop in segments[1:]:
            if current_type is None:
                return None
            if guard_inner is not None and hop in cs.RS_GUARD_ACCESSORS:
                current_type, guard_inner = guard_inner, None
                continue
            guard_inner = None
            # A bare guard-wrapper type not immediately guard-accessed can't
            # continue: its inner is only reachable at runtime and is unrecoverable
            # from the bare name. Bail so the trie fallback resolves the downstream
            # call (matching a direct wrapper-method call's behavior).
            if current_type in cs.RS_GUARD_WRAPPERS:
                return None
            class_qn = self._resolve_rust_type_qn(current_type, module_qn)
            if field_type := self.class_field_types.get(class_qn, {}).get(hop):
                current_type = field_type
                guard_inner = self.class_field_guard_inner.get(class_qn, {}).get(hop)
            elif next_type := self.method_return_types.get(
                f"{class_qn}{cs.SEPARATOR_DOT}{hop}"
            ):
                current_type = next_type
            elif hop not in cs.RS_IDENTITY_METHODS:
                return None
        return current_type

    def _rust_chain_base_type(
        self, segments: list[str], module_qn: str, var_types: dict[str, str]
    ) -> str | None:
        # Base of a flattened chain: a typed local (self/var) when present in
        # var_types, else a free fn called by bare name (`let s = make()`),
        # else the segment itself as a type name -- only useful when there are
        # hops to walk, so a bare unresolved name types nothing.
        base = var_types.get(segments[0]) or self._rust_free_fn_return_type(
            segments[0], module_qn
        )
        if base is not None:
            return base
        return segments[0] if len(segments) > 1 else None

    def _resolve_rust_type_qn(self, type_name: str, module_qn: str) -> str:
        # Resolve a Rust type name to its class-node qn, honoring imports: a `use`
        # target is a raw `::`-path (`crate::cmd::Command`), not a registry qn, so
        # find the registered class node whose simple name matches. Falls back to
        # same-module resolution for a locally-defined type.
        # A fully-qualified inline base (`crate::cmd::Command`) carries its own path.
        if cs.SEPARATOR_DOUBLE_COLON in type_name:
            return self._resolve_rust_import_path(type_name)
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if (target := import_map.get(type_name)) and (
            cs.SEPARATOR_DOUBLE_COLON in target
        ):
            return self._resolve_rust_import_path(target)
        return self._resolve_class_name(type_name, module_qn) or type_name

    def _rust_free_fn_return_type(self, name: str, module_qn: str) -> str | None:
        # Return type of a free fn called by bare name: same-module first, then
        # a `use`-imported fn resolved through its raw `::` path. A type-name
        # base (`Maker::make`) misses here because a bare type is never a
        # recorded key (fns and types share a name only across Rust's separate
        # fn/type namespaces, which idiomatic code never does).
        if return_type := self.method_return_types.get(
            f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        ):
            return return_type
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if (target := import_map.get(name)) and cs.SEPARATOR_DOUBLE_COLON in target:
            fn_qn = self._resolve_rust_import_path(
                target, node_types=(NodeType.FUNCTION,)
            )
            return self.method_return_types.get(fn_qn)
        return None

    def _resolve_rust_import_path(
        self,
        target: str,
        node_types: tuple[NodeType, ...] = (
            NodeType.CLASS,
            NodeType.ENUM,
            NodeType.TYPE,
        ),
    ) -> str:
        # Map a `use` target (`crate::cmd::Command`) to its registry qn. Prefer the
        # candidate whose qn ends with the import's module path (`.cmd.Command`),
        # so two same-named types in different modules disambiguate by path; fall
        # back to the deterministic-min simple-name match when the path (e.g. a
        # crate-root re-export `crate::Command`) can't pinpoint one.
        parts = [
            p
            for p in target.split(cs.SEPARATOR_DOUBLE_COLON)
            if p not in cs.RS_PATH_KEYWORDS
        ]
        if not parts:
            return target
        simple = parts[-1]
        candidates = [
            qn
            for qn in self.function_registry.find_ending_with(simple)
            if self.function_registry.get(qn) in node_types
        ]
        if not candidates:
            return target
        path_suffix = cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
        matching = [qn for qn in candidates if qn.endswith(path_suffix)]
        return min(matching) if matching else min(candidates)

    def _collect_field_types(self, class_qn: str) -> dict[str, str]:
        # Collect member-field types along the inheritance chain so a derived class
        # method can resolve a field inherited from a base. Bases are visited first
        # and the class's own fields applied last, so a derived field shadows a
        # base field of the same name. Guards against inheritance cycles.
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
            case cs.SupportedLanguage.CSHARP:
                return self.csharp_type_inference.build_variable_type_map(caller_node)
            case cs.SupportedLanguage.DART:
                return self.dart_type_inference.build_local_variable_type_map(
                    caller_node
                )
            case cs.SupportedLanguage.LUA:
                return self.lua_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.GO:
                return self.go_type_inference.build_local_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.RUST:
                return self.rust_type_inference.build_local_variable_type_map(
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
