from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..parser_loader import COMBINED_FUNC_CLASS_IMPORT_QUERIES
from ..types_defs import (
    ASTNode,
    CppDefinitionSpan,
    DeferredCppInherit,
    DeferredInherit,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    SimpleNameLookup,
)
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .class_ingest import ClassIngestMixin
from .cpp import CppTypeInferenceEngine
from .cpp.preproc_recovery import parse_with_preproc_recovery
from .csharp_frontend import CallSiteKey, CSharpCallSite
from .dependency_parser import parse_dependencies
from .function_ingest import FunctionIngestMixin
from .handlers import get_handler
from .js_ts.ingest import JsTsIngestMixin
from .utils import safe_decode_with_fallback, sorted_captures

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .handlers import LanguageHandler
    from .import_processor import ImportProcessor


class DefinitionProcessor(
    FunctionIngestMixin,
    ClassIngestMixin,
    JsTsIngestMixin,
):
    _handler: LanguageHandler

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: SimpleNameLookup,
        import_processor: ImportProcessor,
        module_qn_to_file_path: dict[str, Path],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ):
        super().__init__()
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.import_processor = import_processor
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance: dict[str, list[str]] = {}
        # (H) {interface_qn: [implementer_class_qns]} from IMPLEMENTS edges, so the
        # (H) resolver can redirect an interface-typed call `I.m` to the concrete
        # (H) `Impl.m` when I has exactly one first-party implementer (unambiguous).
        self.interface_implementers: dict[str, set[str]] = {}
        # (H) {class_qn: {field_name: bare_type_name}} for C++ member fields, so a
        # (H) member call `field_.method()` in a (possibly out-of-line, cross-file)
        # (H) method resolves via the field's declared type. Populated at class
        # (H) ingestion, read by the type-inference engine at call resolution.
        self.class_field_types: dict[str, dict[str, str]] = {}
        # (H) Java anonymous-class override methods: (anon_method_qn, method_name,
        # (H) base_type_name, module_qn). An anon class `new Base(){ @Override m(){} }`
        # (H) is not modelled as a subclass, so its overrides register under the
        # (H) enclosing class with no OVERRIDES edge and look dead. Recorded at method
        # (H) ingestion, resolved to OVERRIDES edges once every base type is registered.
        self.java_anon_overrides: list[tuple[str, str, str, str]] = []
        # (H) C# override tracking. `csharp_methods` is every C# method qn;
        # (H) `csharp_override_methods` the subset carrying the `override` modifier.
        # (H) C# base-CLASS overrides require `override` (a `new`/implicit-hide
        # (H) member is not an override), so the shared override walk emits a
        # (H) class-parent match only for C# methods in the override subset;
        # (H) interface implementations need no modifier and are unaffected.
        self.csharp_methods: set[str] = set()
        self.csharp_override_methods: set[str] = set()
        # (H) C# partial-class unification. A `partial` type split across files
        # (H) becomes N path-distinct Class nodes; parts sharing a
        # (H) namespace-qualified name are one logical type. Each part qn maps to
        # (H) the SHARED list of all its part qns (grows as parts are ingested),
        # (H) so member/base resolution on any part spans the whole group. The
        # (H) private index groups parts by their namespace-qualified key.
        self.csharp_partial_groups: dict[str, list[str]] = {}
        self._csharp_partial_index: dict[str, list[str]] = {}
        # (H) C# extension methods indexed for call binding: {method simple name:
        # (H) [(method_qn, receiver_type_simple_name)]}. `s.WordCount()` resolves
        # (H) to a `static WordCount(this string s)` whose receiver type matches
        # (H) the call's receiver -- a lookup the instance-hierarchy walk can't
        # (H) make (the method lives on an unrelated static class). Populated at
        # (H) method ingestion, read by the type-inference engine as a fallback.
        self.csharp_extension_methods: dict[str, list[tuple[str, str, str, int]]] = {}
        # (H) C# local functions: {local_fn_qn: (host span key, parameter count)}.
        # (H) The host (method/ctor/enclosing local fn) is pinned by SPAN because
        # (H) at Function-pass time the host method's signatured identity may not
        # (H) be registered yet; the resolver joins the span to function_locations
        # (H) lazily. Bare-name resolution uses this to honor C# scoping: a local
        # (H) fn is callable only from inside its host, and shadows same-name
        # (H) method overloads there (Polly's PredicateBuilder.HandleInner).
        self.csharp_local_functions: dict[str, tuple[FunctionSpanKey, int]] = {}
        # (H) qns of C# methods declared WITH type parameters (`M<T>(X)`), so
        # (H) bare-call resolution can prefer the overload whose genericness
        # (H) matches the callee shape (`M<TResult>(x)` vs `M(x)`) when
        # (H) parameter arity alone cannot tell same-name twins apart.
        self.csharp_generic_methods: set[str] = set()
        # (H) {class qn: declared type-parameter count} for C# generic types,
        # (H) so `Builder` vs `Builder<TResult>` (same simple name) can be told
        # (H) apart when a type reference's written arity is known.
        self.csharp_class_generic_arity: dict[str, int] = {}
        # (H) {method qn: (normalized return type, its written generic arity)}
        # (H) for chained-receiver typing; separate from the cross-language
        # (H) method_return_types because the arity is C#-specific.
        self.csharp_method_return_types: dict[str, tuple[str, int]] = {}
        # (H) C# Roslyn hybrid frontend (issue #738): {(rel_file, type_start_line):
        # (H) {base_simple_name: "class"|"interface"}}. When the opt-in Roslyn
        # (H) frontend ran, split_csharp_bases reads a base's kind here (exact,
        # (H) from the semantic model) instead of guessing by the I-prefix
        # (H) convention; empty when the frontend is off or unavailable.
        self.csharp_base_kinds: dict[tuple[str, int], dict[str, str]] = {}
        # (H) C# Roslyn hybrid frontend (issue #738): per-invocation exact call
        # (H) targets keyed on the callee NAME token location. The C# resolver
        # (H) consults this before any heuristic; MUTATED IN PLACE across runs
        # (H) because the type-inference engine holds a reference.
        self.csharp_call_sites: dict[CallSiteKey, CSharpCallSite] = {}
        # (H) Sites Roslyn resolved to METADATA (external) methods: the resolver
        # (H) returns the external sentinel there instead of letting the
        # (H) name-trie fabricate a first-party edge. Same in-place mutation
        # (H) discipline as csharp_call_sites.
        self.csharp_external_sites: set[CallSiteKey] = set()
        # (H) (rel_file, type_start_line) -> class qn for every ingested C# type,
        # (H) the reverse of the Roslyn fact keys, so partial declaration groups
        # (H) join back to the Pass-2 Class nodes.
        self.csharp_type_locations: dict[tuple[str, int], str] = {}
        # (H) {class_qn: {field_name: inner_type}} for Rust guard-container fields
        # (H) (`state: Mutex<State>` -> {"state": "State"}). The field map above keeps
        # (H) the WRAPPER; this inner is applied only when a receiver chain reaches a
        # (H) lock/read/borrow guard accessor (guards do not deref-coerce).
        self.class_field_guard_inner: dict[str, dict[str, str]] = {}
        # (H) {alias_name: underlying_bare_type} for C++ typedef/using aliases, so a
        # (H) receiver declared with an alias resolves to the aliased class. Collected
        # (H) across all files (an alias in a header is used in a .cc), read by the
        # (H) resolver when mapping a receiver type name to a class.
        self.type_aliases: dict[str, str] = {}
        # (H) {func_or_method_qn: bare_return_type_name} captured at definition
        # (H) ingestion, so a chained call `x.foo().bar()` can resolve `bar` on the
        # (H) type `foo()` returns. Read by the resolver's chained-call path.
        self.method_return_types: dict[str, str] = {}
        # (H) Alias names seen with conflicting underlying types across scopes/files;
        # (H) dropped from type_aliases so their receivers fall back to name-only.
        self._type_alias_conflicts: set[str] = set()
        self._deferred_cpp_methods: list = []
        self._deferred_go_methods: list = []
        self._deferred_cpp_containment: list = []
        self._deferred_parent_links: list = []
        self._deferred_forward_decls: list = []
        # (H) Unnamed JS/TS function expressions held back until the named
        # (H) JS passes have claimed their spans (one node per source function).
        self._deferred_js_anonymous: list = []
        # (H) Macro-invocation-shaped C++ nodes held until every class (incl.
        # (H) rehydrated ones) is known; resolve_deferred_cpp_artifacts decides
        # (H) orphaned-ctor vs macro.
        self._deferred_cpp_artifacts: list = []
        # (H) (module_qn, def start_line) -> (method_qn, class_qn) for every
        # (H) out-of-class C++ method the definition pass bound; Pass-3 call
        # (H) attribution reuses these decisions instead of re-resolving.
        self.cpp_out_of_class_methods: dict[tuple[str, int], tuple[str, str]] = {}
        # (H) (module_qn, def start_line) -> location of EVERY C++ function or
        # (H) method node Pass 2 registered, so Pass-3 caller attribution reuses
        # (H) the registered label/qn instead of re-deriving them structurally
        # (H) (the walks diverge on preprocessor-distorted class bodies).
        self.function_locations: dict[FunctionSpanKey, FunctionLocation] = {}
        # (H) {rel path: [full line spans]} of every C/C++ function/method the
        # (H) tree-sitter pass ingested; the hybrid C++ frontend's macro-use
        # (H) CALLS resolve against these after Pass 2 (see CppDefinitionSpan).
        self.cpp_definition_spans: dict[str, list[CppDefinitionSpan]] = {}
        self._deferred_cpp_inherits: list[DeferredCppInherit] = []
        # (H) Non-C++ INHERITS/IMPLEMENTS held back until every class is
        # (H) registered; resolve_deferred_inherits re-resolves the guesses.
        self._deferred_inherits: list[DeferredInherit] = []
        # (H) C++20 module interfaces declared this run (export module X), and
        # (H) implementation units whose IMPLEMENTS edge waits for its
        # (H) interface to be known.
        self.cpp_module_interfaces: set[str] = set()
        self._deferred_cpp_module_impls: list[tuple[str, str]] = []
        # (H) Inline (non-file) module qns, e.g. Rust `mod x {}`; deferred
        # (H) import verification counts them as real internal targets.
        self.declared_module_qns: set[str] = set()
        # (H) Registered qns that are macro definitions (Rust macro_rules!):
        # (H) macros register as Function nodes but live in a separate namespace,
        # (H) so Pass-3 gates macro-invocation call sites to these targets and
        # (H) fn-namespace call sites away from them.
        self.macro_qns: set[str] = set()
        # (H) {qn: file path} for definitions rehydrated from the graph on an
        # (H) incremental run, whose modules are absent from module_qn_to_file_path
        # (H) (only re-parsed files populate it). _is_cpp_defined falls back to
        # (H) this so cross-file resolution into UNCHANGED headers still works.
        self.rehydrated_definition_paths: dict[str, str] = {}
        self._handler = get_handler(cs.SupportedLanguage.PYTHON)
        self._func_class_captures_cache = func_class_captures_cache

    def _disambiguate_module_qn(self, module_qn: str, file_path: Path) -> str:
        # (H) Two files that share a basename but differ by extension (foo.py /
        # (H) foo.cpp) strip to the same module qn. Append the extension to the
        # (H) later one so their module nodes and all derived class/method qns stay
        # (H) distinct instead of colliding under the qualified_name constraint.
        existing = self.module_qn_to_file_path.get(module_qn)
        if existing is None or existing == file_path:
            return module_qn
        return (
            f"{module_qn}{cs.SEPARATOR_DOT}{file_path.suffix.lstrip(cs.SEPARATOR_DOT)}"
        )

    def process_file(
        self,
        file_path: Path,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        structural_elements: dict[Path, str | None],
        source_bytes: bytes | None = None,
        pre_parsed: tuple[ASTNode, dict[str, list] | None] | None = None,
    ) -> tuple[ASTNode, cs.SupportedLanguage] | None:
        if isinstance(file_path, str):
            file_path = Path(file_path)
        relative_path = cached_relative_path(file_path, self.repo_path)
        relative_path_str = relative_path.as_posix()
        logger.info(
            ls.DEF_PARSING_AST.format(language=language, path=relative_path_str)
        )

        try:
            if language not in queries:
                logger.warning(
                    ls.DEF_UNSUPPORTED_LANGUAGE.format(
                        language=language, path=file_path
                    )
                )
                return None

            self._handler = get_handler(language)
            if pre_parsed is not None:
                root_node, pre_combined_captures = pre_parsed
            else:
                if source_bytes is None:
                    source_bytes = file_path.read_bytes()
                lang_queries = queries[language]
                parser = lang_queries.get(cs.KEY_PARSER)
                if not parser:
                    logger.warning(ls.DEF_NO_PARSER.format(language=language))
                    return None
                tree = parse_with_preproc_recovery(parser, source_bytes, language)
                root_node = tree.root_node
                pre_combined_captures = None

            module_qn = cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name in (cs.INIT_PY, cs.MOD_RS):
                module_qn = cs.SEPARATOR_DOT.join(
                    [self.project_name] + list(relative_path.parent.parts)
                )
            module_qn = self._disambiguate_module_qn(module_qn, file_path)
            self.module_qn_to_file_path[module_qn] = file_path

            self.ingestor.ensure_node_batch(
                cs.NodeLabel.MODULE,
                {
                    cs.KEY_QUALIFIED_NAME: module_qn,
                    cs.KEY_NAME: file_path.name,
                    cs.KEY_PATH: relative_path_str,
                    cs.KEY_ABSOLUTE_PATH: cached_resolve_posix(file_path),
                },
            )

            parent_rel_path = relative_path.parent
            parent_container_qn = structural_elements.get(parent_rel_path)
            parent_label, parent_key, parent_val = (
                (cs.NodeLabel.PACKAGE, cs.KEY_QUALIFIED_NAME, parent_container_qn)
                if parent_container_qn
                else (
                    (cs.NodeLabel.FOLDER, cs.KEY_PATH, parent_rel_path.as_posix())
                    if parent_rel_path != Path(".")
                    else (cs.NodeLabel.PROJECT, cs.KEY_NAME, self.project_name)
                )
            )
            self.ingestor.ensure_relationship_batch(
                (parent_label, parent_key, parent_val),
                cs.RelationshipType.CONTAINS_MODULE,
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            )

            if pre_combined_captures is not None:
                combined_captures = pre_combined_captures
            else:
                combined_captures = None
                combined_query = COMBINED_FUNC_CLASS_IMPORT_QUERIES.get(language)
                if combined_query:
                    cursor = QueryCursor(combined_query)
                    combined_captures = sorted_captures(cursor, root_node)
            if self._func_class_captures_cache is not None and combined_captures:
                cache_entry: dict[str, list] = {}
                for key in (cs.CAPTURE_FUNCTION, cs.CAPTURE_CLASS, cs.CAPTURE_CALL):
                    if key in combined_captures:
                        cache_entry[key] = combined_captures[key]
                if cache_entry:
                    self._func_class_captures_cache[file_path] = cache_entry

            self.import_processor.parse_imports(
                root_node,
                module_qn,
                language,
                queries,
                pre_captures=combined_captures,
            )
            if language in cs.JS_TS_LANGUAGES:
                self._ingest_missing_import_patterns(
                    root_node, module_qn, language, queries
                )
            if language == cs.SupportedLanguage.CPP:
                self._ingest_cpp_module_declarations(root_node, module_qn, file_path)
                CppTypeInferenceEngine().collect_type_aliases(
                    root_node, self.type_aliases, self._type_alias_conflicts
                )
            self._ingest_all_functions(
                root_node,
                module_qn,
                language,
                queries,
                combined_captures=combined_captures,
            )
            self._ingest_classes_and_methods(
                root_node,
                module_qn,
                language,
                queries,
                combined_captures=combined_captures,
            )
            if language in cs.JS_TS_LANGUAGES:
                self._ingest_object_literal_methods(
                    root_node, module_qn, language, queries
                )
                self._ingest_commonjs_exports(root_node, module_qn, language, queries)
                self._ingest_es6_exports(root_node, module_qn, language, queries)
                self._ingest_assignment_arrow_functions(
                    root_node, module_qn, language, queries
                )
                self._ingest_prototype_inheritance(
                    root_node, module_qn, language, queries
                )
                # (H) Named passes above have claimed their function nodes;
                # (H) only genuinely anonymous spans (callbacks, IIFEs) still
                # (H) need their held-back registration.
                self._flush_deferred_js_anonymous()

            return (root_node, language)

        except Exception as e:
            logger.error(ls.DEF_PARSE_FAILED.format(path=file_path, error=e))
            return None

    def process_dependencies(self, filepath: Path) -> None:
        logger.info(ls.DEF_PARSING_DEPENDENCY.format(path=filepath))

        dependencies = parse_dependencies(filepath)
        for dep in dependencies:
            self._add_dependency(dep.name, dep.spec, dep.properties)

    def _add_dependency(
        self, dep_name: str, dep_spec: str, properties: dict[str, str] | None = None
    ) -> None:
        if not dep_name or dep_name.lower() in cs.EXCLUDED_DEPENDENCY_NAMES:
            return

        logger.info(ls.DEF_FOUND_DEPENDENCY.format(name=dep_name, spec=dep_spec))
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.EXTERNAL_PACKAGE, {cs.KEY_NAME: dep_name}
        )

        rel_properties = {cs.KEY_VERSION_SPEC: dep_spec} if dep_spec else {}
        if properties:
            rel_properties |= properties

        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.PROJECT, cs.KEY_NAME, self.project_name),
            cs.RelationshipType.DEPENDS_ON_EXTERNAL,
            (cs.NodeLabel.EXTERNAL_PACKAGE, cs.KEY_NAME, dep_name),
            properties=rel_properties,
        )

    def _get_docstring(self, node: ASTNode) -> str | None:
        body_node = node.child_by_field_name(cs.FIELD_BODY)
        if not body_node or not body_node.children:
            return None
        first_statement = body_node.children[0]
        if (
            first_statement.type == cs.TS_PY_EXPRESSION_STATEMENT
            and first_statement.children[0].type == cs.TS_PY_STRING
        ):
            text = first_statement.children[0].text
            if text is not None:
                result: str = safe_decode_with_fallback(
                    first_statement.children[0]
                ).strip(cs.DOCSTRING_STRIP_CHARS)
                return result
        return None

    def _extract_decorators(self, node: ASTNode) -> list[str]:
        return self._handler.extract_decorators(node)
