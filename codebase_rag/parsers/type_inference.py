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
from .csharp_frontend import CallSiteKey
from .dart.type_inference import DartTypeInferenceEngine
from .frontends.protocol import ResolvedCallSite
from .go import GoTypeInferenceEngine
from .import_processor import ImportProcessor
from .java import JavaTypeInferenceEngine
from .js_ts import JsTypeInferenceEngine
from .lua import LuaTypeInferenceEngine
from .py import PythonTypeInferenceEngine, resolve_class_name
from .rs import RustTypeInferenceEngine
from .rs import utils as rs_utils

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
        "class_field_element_types",
        "method_return_types",
        "go_function_return_types",
        "go_call_sites",
        "go_external_sites",
        "java_call_sites",
        "java_external_sites",
        "python_call_sites",
        "python_external_sites",
        "csharp_partial_groups",
        "csharp_extension_methods",
        "csharp_call_sites",
        "csharp_arg_flows",
        "csharp_bind_flows",
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
        "dart_extends_type_args",
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
        class_field_element_types: dict[str, dict[str, str]] | None = None,
        method_return_types: dict[str, str] | None = None,
        go_function_return_types: dict[str, str] | None = None,
        go_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        go_external_sites: set[CallSiteKey] | None = None,
        java_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        java_external_sites: set[CallSiteKey] | None = None,
        python_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        python_external_sites: set[CallSiteKey] | None = None,
        csharp_partial_groups: dict[str, list[str]] | None = None,
        csharp_extension_methods: dict[str, list[tuple[str, str, str, int]]]
        | None = None,
        csharp_call_sites: dict[CallSiteKey, ResolvedCallSite] | None = None,
        csharp_arg_flows: dict[CallSiteKey, dict[int, frozenset[str]]] | None = None,
        csharp_bind_flows: dict[CallSiteKey, frozenset[str]] | None = None,
        csharp_external_sites: set[CallSiteKey] | None = None,
        csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] | None = None,
        csharp_generic_methods: set[str] | None = None,
        csharp_class_generic_arity: dict[str, int] | None = None,
        csharp_method_return_types: dict[str, tuple[str, int]] | None = None,
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
        dart_extends_type_args: dict[str, list[str]] | None = None,
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
        # DefinitionProcessor's map, empty at construction and populated later during
        # ingestion. `or {}` would swap it for a new dict and silently lose every
        # field type written afterward.
        self.class_field_types = (
            class_field_types if class_field_types is not None else {}
        )
        # Shared reference (as with class_field_types): Rust guard-field inner types
        # (`Shared.state` -> State for a `Mutex<State>` field), applied only at a
        # guard-accessor hop so a direct wrapper call isn't mis-resolved to it.
        self.class_field_guard_inner = (
            class_field_guard_inner if class_field_guard_inner is not None else {}
        )
        # Shared reference (as with class_field_types): Rust sequence-field
        # element types (`Pool.workers` -> Worker for a `Vec<Worker>` field),
        # applied only when an iterator adaptor's closure parameter binds the
        # element (issue #1045).
        self.class_field_element_types = (
            class_field_element_types if class_field_element_types is not None else {}
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
        # Shared references (as with csharp_call_sites): the go/types call-site
        # facts and external sites, populated after construction and read by the
        # Go resolver's semantic path (issue #1179).
        self.go_call_sites = go_call_sites if go_call_sites is not None else {}
        self.go_external_sites = (
            go_external_sites if go_external_sites is not None else set()
        )
        self.java_call_sites = java_call_sites if java_call_sites is not None else {}
        self.java_external_sites = (
            java_external_sites if java_external_sites is not None else set()
        )
        # Shared references, same discipline: the Jedi call-site facts and
        # external proofs (issue #1183), populated after construction.
        self.python_call_sites = (
            python_call_sites if python_call_sites is not None else {}
        )
        self.python_external_sites = (
            python_external_sites if python_external_sites is not None else set()
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
        # Shared reference (as with csharp_call_sites): Roslyn argument-flow
        # facts, read by the C# path of the lean flow walk (issue #1187).
        self.csharp_arg_flows = csharp_arg_flows if csharp_arg_flows is not None else {}
        self.csharp_bind_flows = (
            csharp_bind_flows if csharp_bind_flows is not None else {}
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
        # Shared reference (as with class_field_types): Dart `extends Base<T>`
        # type arguments per class qn, read by the resolver's undeclared
        # receiver fallback (#875).
        self.dart_extends_type_args = (
            dart_extends_type_args if dart_extends_type_args is not None else {}
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
            self._go_type_inference = GoTypeInferenceEngine(
                go_call_sites=self.go_call_sites,
                go_external_sites=self.go_external_sites,
                function_locations=self.function_locations,
                module_qn_to_file_path=self.module_qn_to_file_path,
                repo_path=self.repo_path,
            )
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
                java_call_sites=self.java_call_sites,
                java_external_sites=self.java_external_sites,
                function_locations=self.function_locations,
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
        # base so a bare `field_.method()` receiver resolves; a same-named parameter
        # or local shadows a field, so the local map wins on conflict.
        fields = self._collect_field_types(class_context) if class_context else {}
        if fields:
            local = {**fields, **local}
        if language == cs.SupportedLanguage.GO:
            self._enrich_go_call_locals(caller_node, module_qn, local)
        elif language == cs.SupportedLanguage.RUST:
            if class_context:
                # Rust member calls carry the explicit `self` receiver: type `self`
                # to the impl target (so `self.accept()` dispatches) and each
                # `self.<field>` to its field type (`self.shutdown.is_shutdown()`
                # hops through it). setdefault: a same-named local wins.
                local.setdefault(cs.KEYWORD_SELF, class_context)
                for field, ftype in fields.items():
                    local.setdefault(
                        f"{cs.KEYWORD_SELF}{cs.SEPARATOR_DOT}{field}", ftype
                    )
            # Substitute BEFORE enrichment: params, lets, and fields spell
            # generic names from the caller's own scope chain, but an
            # enriched call-return type (`let x = Maker::make()` with
            # `fn make() -> M`) spells the CALLEE's generic parameter, which
            # the caller's bounds must not capture. Sole exception: a
            # `self.<method>()` return spells the caller's own impl-header
            # generics, handled inside enrichment.
            self._substitute_rust_generic_bounds(caller_node, module_qn, local)
            self._enrich_rust_call_locals(caller_node, module_qn, local)
        elif language == cs.SupportedLanguage.DART:
            self._enrich_dart_call_locals(caller_node, local)
        return local

    def collect_rust_span_bindings(
        self, caller_node: ASTNode, module_qn: str, class_context: str | None
    ) -> list[tuple[int, int, str, str]]:
        """Span-scoped Rust bindings overlaid at each call's position.

        Match-arm variant bindings plus iterator-adaptor closure-parameter
        bindings (issue #1045): both rebind one name to different types at
        different byte ranges, which the flat map cannot express. Element
        types come from the caller's own collection-typed params/lets plus
        its class's sequence fields, keyed `self.<field>` exactly as the
        flat map spells field receivers. Closure bindings whose type does
        NOT resolve to a registered class from this module are dropped: a
        type parameter (`Vec<T>`) or a name invisible here would let the
        external-receiver guard delete the edge the trie fallback still
        finds, so an unresolvable binding is strictly worse than none.
        """
        engine = self.rust_type_inference
        bindings = engine.collect_match_arm_bindings(caller_node)
        element_entries = engine.collect_element_entries(caller_node)
        if class_context:
            span = (caller_node.start_byte, caller_node.end_byte)
            for field, elem in self.class_field_element_types.get(
                class_context, {}
            ).items():
                element_entries.append(
                    (f"{cs.KEYWORD_SELF}{cs.SEPARATOR_DOT}{field}", elem, *span, 0)
                )
        bindings.extend(
            binding
            for binding in engine.collect_closure_param_bindings(
                caller_node, element_entries
            )
            if self._rust_binding_type_resolves(binding[3], module_qn)
        )
        return bindings

    def _substitute_rust_generic_bounds(
        self, caller_node: ASTNode, module_qn: str, var_types: dict[str, str]
    ) -> None:
        # A receiver typed as a bare generic parameter (`m: M`, a field `M` of
        # an `impl<M: Matcher>` block) dispatches through the parameter's trait
        # bound, so rewrite the recorded type to the qn of the first bound that
        # resolves strictly first-party (issue #1047). A parameter whose bounds
        # resolve to nothing keeps the generic name: the known-external
        # receiver guard then suppresses the name-based fallback instead of
        # letting it fabricate an edge onto an unrelated same-named method.
        if not var_types:
            # Nothing recorded to rewrite, so the ancestor walk that collects
            # the bounds is pure waste.
            return
        bounds = self.rust_type_inference.collect_generic_bounds(caller_node)
        if not bounds:
            return
        for name in var_types:
            self._apply_rust_generic_bound(bounds, module_qn, var_types, name)

    def _apply_rust_generic_bound(
        self,
        bounds: dict[str, list[str]],
        module_qn: str,
        var_types: dict[str, str],
        name: str,
    ) -> None:
        for spelling in bounds.get(var_types[name], ()):
            if trait_qn := self._rust_bound_trait_qn(spelling, module_qn):
                var_types[name] = trait_qn
                break

    def _rust_bound_trait_qn(self, spelling: str, module_qn: str) -> str | None:
        # Resolve a bound spelling STRICTLY to a registered first-party qn;
        # anything looser fabricates edges: the simple-name fallbacks would
        # bind `use std::io::Write` + `W: Write` (or a bare prelude `Clone`)
        # to an arbitrary first-party type sharing the leaf name. The qn goes
        # into the map directly, so the consumer cannot re-resolve it fuzzily.
        if cs.SEPARATOR_DOUBLE_COLON in spelling:
            # A scoped bound substitutes only on an exact module-path match.
            parts = [
                p
                for p in spelling.split(cs.SEPARATOR_DOUBLE_COLON)
                if p not in cs.RS_PATH_KEYWORDS
            ]
            qn = self._resolve_rust_import_path(
                spelling, node_types=(NodeType.INTERFACE,)
            )
            suffix = cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
            if qn.endswith(suffix) and self._rust_registered_trait(qn):
                return qn
            return None
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if (target := import_map.get(spelling)) is not None:
            # An external `use` target is a raw `::` path; a first-party one
            # arrives as an already-resolved project qn.
            if cs.SEPARATOR_DOUBLE_COLON in target:
                return None
            return target if self._rust_registered_trait(target) else None
        # No import in scope: a bare name is only visible when defined in
        # this module (a prelude trait lands here and resolves to None).
        local_qn = f"{module_qn}{cs.SEPARATOR_DOT}{spelling}"
        return local_qn if self._rust_registered_trait(local_qn) else None

    def _rust_registered_trait(self, qn: str) -> bool:
        # A Rust bound always names a trait, which registers as INTERFACE;
        # a same-named struct/enum must never satisfy it.
        return self.function_registry.get(qn) == NodeType.INTERFACE

    def _rust_binding_type_resolves(self, type_name: str, module_qn: str) -> bool:
        qn = self._resolve_rust_type_qn(type_name, module_qn)
        return self.function_registry.get(qn) is not None

    def _enrich_dart_call_locals(
        self, caller_node: ASTNode, var_types: dict[str, str]
    ) -> None:
        # A local bound from a class-qualified call (`var s = Base.member(args)`)
        # was heuristically typed as Base; when the member is a REGISTERED method
        # with a recorded return type, that type wins (a named constructor or
        # same-class factory keeps Base, a `static String describe()` local becomes
        # a String whose member calls then drop as external). A registered member
        # WITHOUT a recorded return (void) untypes the local; an unregistered member
        # keeps the heuristic.
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
        # real type (node) instead of the enclosing class's same-named method.
        # Resolves the callee selector hop by hop: base local type, struct-field
        # types for middle hops, then the final method's recorded return type. Only
        # fills names not already typed.
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
        # A plain function (`['f']`) types from the free-fn return map: same-module
        # first, then a same-package sibling file (Go package scope spans the
        # directory, viper's remote.go shape).
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
        # Same module (file) first; then the enclosing package's sibling files
        # (same parent dir), since Go free functions are package-scoped, not
        # file-scoped. The sibling lookup goes through a lazily rebuilt (package,
        # name) index: the shared map fills DURING ingestion, so an init-time index
        # would be empty; the size check rebuilds only when entries were added.
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
        # later `cmd.apply()` resolves to the real type, not the ambiguous name-only
        # trie fallback. Only fills names not already typed.
        bounds: dict[str, list[str]] | None = None
        for (
            name,
            segments,
            call_point,
        ) in self.rust_type_inference.collect_call_var_bindings(caller_node):
            if name in var_types:
                continue
            if return_type := self._infer_rust_call_return_type(
                segments, module_qn, var_types, call_point
            ):
                var_types[name] = return_type
                if len(segments) == 2 and segments[0] == cs.KEYWORD_SELF:
                    # A `let m = self.get()` callee lives in the caller's own
                    # impl, whose header generics a method cannot shadow
                    # (E0403): a generic return spells the CALLER's own
                    # parameter, so its bound applies. Any other callee's
                    # generic return stays uncaptured (issue #1047).
                    if bounds is None:
                        bounds = self.rust_type_inference.collect_generic_bounds(
                            caller_node
                        )
                    self._apply_rust_generic_bound(bounds, module_qn, var_types, name)

    def _infer_rust_call_return_type(
        self,
        segments: list[str],
        module_qn: str,
        var_types: dict[str, str],
        call_point: int | None,
    ) -> str | None:
        # Walk a flattened chain to the type it yields:
        #   ['Command','from_frame']  -> base type Command, method from_frame
        #   ['self','shared','state','lock','unwrap'] -> base local self (Db),
        #     field shared (Arc<Shared>->Shared), field state (Mutex<State>, guard
        #     inner State), lock unwraps the guard -> State, unwrap identity -> State.
        # Each hop tries guard-unwrap (a guard accessor right after a guard-wrapped
        # field) -> field-type -> method-return -> identity.
        if not segments:
            return None
        current_type = self._rust_chain_base_type(
            segments, module_qn, var_types, call_point
        )
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
            # continue: its inner is only reachable at runtime, unrecoverable from
            # the bare name. Bail so the trie fallback resolves the downstream call
            # (matching a direct wrapper-method call).
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
        self,
        segments: list[str],
        module_qn: str,
        var_types: dict[str, str],
        call_point: int | None,
    ) -> str | None:
        # Base of a flattened chain: a typed local (self/var) when present in
        # var_types, else a free fn called by bare name (`let s = make()`), else the
        # segment itself as a type name, useful only when there are hops to walk, so
        # a bare unresolved name types nothing.
        base = var_types.get(segments[0]) or self._rust_free_fn_return_type(
            segments[0], module_qn, call_point
        )
        if base is not None:
            return base
        return segments[0] if len(segments) > 1 else None

    def _resolve_rust_type_qn(self, type_name: str, module_qn: str) -> str:
        # Resolve a Rust type name to its class-node qn, honoring imports: an
        # external `use` target is a raw `::`-path (`std::io::Read`) matched by
        # simple name, a local one an already-resolved project qn. Falls back to
        # same-module resolution for a locally-defined type. A fully-qualified
        # inline base (`crate::cmd::Command`) carries its own path.
        if cs.SEPARATOR_DOUBLE_COLON in type_name:
            return self._resolve_rust_import_path(type_name)
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if target := import_map.get(type_name):
            if cs.SEPARATOR_DOUBLE_COLON in target:
                return self._resolve_rust_import_path(target)
            # A crate::/super::/self:: use target arrives as an already
            # resolved project qn; use it when it names a registered type.
            # Otherwise fall through with require_registered so the SAME
            # unregistered map value cannot come back verbatim.
            if self.function_registry.get(target) is not None:
                return target
            return (
                resolve_class_name(
                    type_name,
                    module_qn,
                    self.import_processor,
                    self.function_registry,
                    require_registered=True,
                )
                or type_name
            )
        return self._resolve_class_name(type_name, module_qn) or type_name

    def _rust_free_fn_return_type(
        self, name: str, module_qn: str, call_point: int | None
    ) -> str | None:
        # Return type of a free fn called by bare name: same-module first, then a
        # `use`-imported fn resolved through its raw `::` path. A type-name base
        # (`Maker::make`) misses here because a bare type is never a recorded key
        # (fns and types share a name only across Rust's separate fn/type namespaces,
        # which idiomatic code never does).
        # A body-local fn shadows the module's own for the block it is written
        # in, and it registers under a `@<line>` variant when the module item
        # owns the natural qn, so the site decides which of the two is meant
        # (issue #1069).
        if shadowing := rs_utils.block_item_at(
            self.import_processor.rust_block_items.get(module_qn, ()),
            self.function_registry,
            name,
            call_point,
        ):
            # The site names the block item, so the module item of that name is
            # not what is meant, whatever comes of the block item's own return.
            # A unit or tuple return records nothing, and a generic return
            # records a type parameter that resolves to no class here; either
            # way the answer is no type rather than the module item's, since
            # the known-external guard would delete the edge the trie fallback
            # still finds and an unresolvable binding is worse than none.
            shadow_type = self.method_return_types.get(shadowing)
            if shadow_type and self._rust_binding_type_resolves(shadow_type, module_qn):
                return shadow_type
            return None
        if return_type := self.method_return_types.get(
            f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        ):
            return return_type
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if target := import_map.get(name):
            if cs.SEPARATOR_DOUBLE_COLON in target:
                target = self._resolve_rust_import_path(
                    target, node_types=(NodeType.FUNCTION,)
                )
            # A crate::/super::/self:: use target is already a project qn.
            return self.method_return_types.get(target)
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
        # candidate whose qn ends with the import's module path (`.cmd.Command`), so
        # two same-named types in different modules disambiguate by path; fall back
        # to the deterministic-min simple-name match when the path (a crate-root
        # re-export `crate::Command`) can't pinpoint one.
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
        # same-named base field. Guards against inheritance cycles.
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
