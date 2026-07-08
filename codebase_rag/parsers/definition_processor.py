from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..parser_loader import COMBINED_FUNC_CLASS_IMPORT_QUERIES
from ..types_defs import ASTNode, FunctionRegistryTrieProtocol, SimpleNameLookup
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .class_ingest import ClassIngestMixin
from .cpp import CppTypeInferenceEngine
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
        # (H) (module_qn, def start_line) -> (method_qn, class_qn) for every
        # (H) out-of-class C++ method the definition pass bound; Pass-3 call
        # (H) attribution reuses these decisions instead of re-resolving.
        self.cpp_out_of_class_methods: dict[tuple[str, int], tuple[str, str]] = {}
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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
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
                tree = parser.parse(source_bytes)
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
