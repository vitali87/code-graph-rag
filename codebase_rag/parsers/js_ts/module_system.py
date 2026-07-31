from __future__ import annotations

import textwrap
from abc import abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Language, QueryCursor

from ... import constants as cs
from ... import logs as ls
from ...types_defs import ASTNode
from ..utils import (
    function_span_key,
    get_cached_query,
    ingest_exported_function,
    safe_decode_text,
    safe_decode_with_fallback,
    sorted_captures,
)
from .utils import get_js_ts_language_obj

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import (
        FunctionLocation,
        FunctionRegistryTrieProtocol,
        FunctionSpanKey,
        LanguageQueries,
        SimpleNameLookup,
    )
    from ..import_processor import ImportProcessor


class JsTsModuleSystemMixin:
    __slots__ = ("_processed_imports", "_pending_direct_module_exports")
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    function_locations: dict[FunctionSpanKey, FunctionLocation]
    _processed_imports: set[str]

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    @abstractmethod
    def _is_export_inside_function(self, node: ASTNode) -> bool: ...

    # Span-claim protocol (implemented by FunctionIngestMixin): one source
    # function must mint exactly one node PER NAME, so every JS/TS registration
    # path checks the claim before registering and claims after (deliberate
    # different-name twins still register).
    @abstractmethod
    def _span_claimed_for_qn(
        self, module_qn: str, func_node: ASTNode, candidate_qn: str
    ) -> bool: ...

    @abstractmethod
    def _claim_function_span(
        self, module_qn: str, func_node: ASTNode, label: str, qualified_name: str
    ) -> None: ...

    def __init__(self) -> None:
        self._processed_imports = set()
        self._pending_direct_module_exports: list[tuple[ASTNode, bool]] = []

    def _ingest_missing_import_patterns(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        language_obj = get_js_ts_language_obj(language, queries)
        if not language_obj:
            return

        try:
            try:
                query = get_cached_query(language_obj, cs.JS_COMMONJS_DESTRUCTURE_QUERY)
                cursor = QueryCursor(query)
                captures = sorted_captures(cursor, root_node)

                variable_declarators = captures.get(cs.CAPTURE_VARIABLE_DECLARATOR, [])

                for declarator in variable_declarators:
                    self._process_variable_declarator_for_commonjs(
                        declarator, module_qn, language
                    )

            except Exception as e:
                logger.debug(ls.JS_COMMONJS_DESTRUCTURE_FAILED, error=e)

        except Exception as e:
            logger.debug(ls.JS_MISSING_IMPORT_PATTERNS_FAILED, error=e)

    def _extract_require_module_name(self, declarator: ASTNode) -> str | None:
        name_node = declarator.child_by_field_name(cs.FIELD_NAME)
        if not name_node or name_node.type != cs.TS_OBJECT_PATTERN:
            return None

        value_node = declarator.child_by_field_name(cs.FIELD_VALUE)
        if not value_node or value_node.type != cs.TS_CALL_EXPRESSION:
            return None

        function_node = value_node.child_by_field_name(cs.FIELD_FUNCTION)
        if not function_node or function_node.type != cs.TS_IDENTIFIER:
            return None

        if (
            function_node.text is None
            or safe_decode_text(function_node) != cs.JS_REQUIRE_KEYWORD
        ):
            return None

        arguments_node = value_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if not arguments_node or not arguments_node.children:
            return None

        module_string_node = next(
            (c for c in arguments_node.children if c.type == cs.TS_STRING),
            None,
        )
        if not module_string_node or module_string_node.text is None:
            return None

        return safe_decode_with_fallback(module_string_node).strip("'\"")

    def _process_destructured_child(
        self,
        child: ASTNode,
        module_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN:
            if child.text is not None and (name := safe_decode_text(child)):
                self._process_commonjs_import(name, module_name, module_qn, language)
            return

        if child.type != cs.TS_PAIR_PATTERN:
            return

        key_node = child.child_by_field_name(cs.FIELD_KEY)
        value_node = child.child_by_field_name(cs.FIELD_VALUE)

        if not (key_node and key_node.type == cs.TS_PROPERTY_IDENTIFIER):
            return
        if not (value_node and value_node.type == cs.TS_IDENTIFIER):
            return
        if value_node.text is None:
            return

        if alias_name := safe_decode_text(value_node):
            self._process_commonjs_import(alias_name, module_name, module_qn, language)

    def _process_variable_declarator_for_commonjs(
        self, declarator: ASTNode, module_qn: str, language: cs.SupportedLanguage
    ) -> None:
        try:
            module_name = self._extract_require_module_name(declarator)
            if not module_name:
                return

            name_node = declarator.child_by_field_name(cs.FIELD_NAME)
            if not name_node:
                return

            for child in name_node.children:
                self._process_destructured_child(
                    child, module_name, module_qn, language
                )

        except Exception as e:
            logger.debug(ls.JS_COMMONJS_VAR_DECLARATOR_FAILED, error=e)

    def _process_commonjs_import(
        self,
        imported_name: str,
        module_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        try:
            # A destructured `require()` reads a dual-package exports map from
            # the require side, so the CommonJS condition, not the ESM one,
            # names the source module.
            resolved_source_module = self.import_processor._resolve_js_module_path(
                module_name, module_qn, require=True
            )

            import_key = f"{module_qn}->{resolved_source_module}"
            if import_key not in self._processed_imports:
                # Route through the same deferred verification as every
                # other IMPORTS edge: an internal target must be a real
                # module, an external one gets its ExternalModule node at
                # flush (issue #652: this path emitted directly and was the
                # last source of phantom import targets).
                self.import_processor.defer_import_edge(
                    module_qn, resolved_source_module, language
                )

                logger.debug(
                    ls.JS_MISSING_IMPORT_PATTERN,
                    module_qn=module_qn,
                    imported_name=imported_name,
                    resolved_source_module=resolved_source_module,
                )

                self._processed_imports.add(import_key)

        except Exception as e:
            logger.debug(
                ls.JS_COMMONJS_IMPORT_FAILED, imported_name=imported_name, error=e
            )

    def _ingest_export_function(
        self,
        export_function: ASTNode,
        function_name: str,
        module_qn: str,
        export_type: str,
    ) -> None:
        if self._span_claimed_for_qn(
            module_qn,
            export_function,
            f"{module_qn}{cs.SEPARATOR_DOT}{function_name}",
        ):
            return
        function_qn = ingest_exported_function(
            export_function,
            function_name,
            module_qn,
            export_type,
            self.ingestor,
            self.function_registry,
            self.simple_name_lookup,
            self._get_docstring,
            self._is_export_inside_function,
            self.module_qn_to_file_path.get(module_qn),
            self.repo_path,
        )
        if function_qn is not None:
            self._claim_function_span(
                module_qn,
                export_function,
                cs.NodeLabel.FUNCTION.value,
                function_qn,
            )

    def _process_exports_pattern(
        self,
        exports_objs: list[ASTNode],
        export_names: list[ASTNode],
        export_functions: list[ASTNode],
        module_qn: str,
    ) -> None:
        for exports_obj, export_name, export_function in zip(
            exports_objs, export_names, export_functions
        ):
            if not (exports_obj.text and export_name.text):
                continue
            if safe_decode_text(exports_obj) != cs.JS_EXPORTS_KEYWORD:
                continue
            if function_name := safe_decode_text(export_name):
                self._ingest_export_function(
                    export_function,
                    function_name,
                    module_qn,
                    cs.JS_EXPORT_TYPE_COMMONJS,
                )

    def _process_module_exports_pattern(
        self,
        module_objs: list[ASTNode],
        exports_props: list[ASTNode],
        export_names: list[ASTNode],
        export_functions: list[ASTNode],
        module_qn: str,
    ) -> None:
        for module_obj, exports_prop, export_name, export_function in zip(
            module_objs, exports_props, export_names, export_functions
        ):
            if not (module_obj.text and exports_prop.text and export_name.text):
                continue
            if safe_decode_text(module_obj) != cs.JS_MODULE_KEYWORD:
                continue
            if safe_decode_text(exports_prop) != cs.JS_EXPORTS_KEYWORD:
                continue
            if function_name := safe_decode_text(export_name):
                self._ingest_export_function(
                    export_function,
                    function_name,
                    module_qn,
                    cs.JS_EXPORT_TYPE_COMMONJS_MODULE,
                )

    def _ingest_direct_module_export(
        self,
        root_node: ASTNode,
        module_qn: str,
        language_obj: Language,
    ) -> list[tuple[ASTNode, bool]]:
        # `module.exports = function (...) {...}` makes the WHOLE module one
        # function; `module.exports = function (...) {...}(args)` (with or
        # without parens, fastify's generated error-serializer) exports the
        # IIFE's RETURN value and runs the wrapper at module load. Collect
        # the function nodes now; the caller finalises AFTER the deferred
        # anonymous flush, resolving each node's qn by its own SOURCE SPAN so
        # a name collision or refused registration can never point the edge
        # or the alias map at an unrelated namesake.
        pending: list[tuple[ASTNode, bool]] = []
        try:
            cursor = QueryCursor(
                get_cached_query(language_obj, cs.JS_COMMONJS_DIRECT_EXPORT_QUERY)
            )
            captures = sorted_captures(cursor, root_node)
        except Exception as e:
            logger.debug(ls.JS_COMMONJS_EXPORTS_QUERY_FAILED, error=e)
            return pending
        for module_obj, exports_prop, export_function in zip(
            captures.get(cs.CAPTURE_MODULE_OBJ, []),
            captures.get(cs.CAPTURE_EXPORTS_PROP, []),
            captures.get(cs.CAPTURE_EXPORT_FUNCTION, []),
        ):
            if safe_decode_text(module_obj) != cs.JS_MODULE_KEYWORD:
                continue
            if safe_decode_text(exports_prop) != cs.JS_EXPORTS_KEYWORD:
                continue
            if self._is_export_inside_function(export_function):
                # Assigned when the enclosing function runs, not at module
                # load: neither a load-time call nor the module's export.
                continue
            entry = self._pending_direct_export_entry(export_function, module_qn)
            if entry is not None:
                pending.append(entry)
        return pending

    def _pending_direct_export_entry(
        self, export_function: ASTNode, module_qn: str
    ) -> tuple[ASTNode, bool] | None:
        if export_function.type == cs.TS_CALL_EXPRESSION:
            callee: ASTNode | None = export_function.child_by_field_name(
                cs.FIELD_FUNCTION
            )
            while callee is not None and callee.type == cs.TS_PARENTHESIZED_EXPRESSION:
                callee = callee.named_children[0] if callee.named_children else None
            if callee is None or callee.type not in (
                cs.TS_FUNCTION_EXPRESSION,
                cs.TS_ARROW_FUNCTION,
            ):
                # Any other call expression is not a function export.
                return None
            return callee, True
        # The function IS the export: register it (own name, or its position
        # when anonymous) marked exported. A refusal (span already claimed by
        # the node's earlier registration) is fine: finalisation reads the
        # claimed span either way.
        name_node = export_function.child_by_field_name(cs.FIELD_NAME)
        function_name = safe_decode_text(name_node) if name_node is not None else None
        if not function_name:
            row, col = export_function.start_point
            function_name = f"{cs.PREFIX_ANONYMOUS}{row}_{col}"
        self._ingest_export_function(
            export_function,
            function_name,
            module_qn,
            cs.JS_EXPORT_TYPE_COMMONJS_MODULE,
        )
        return export_function, False

    def _finalise_direct_module_exports(
        self, module_qn: str, pending: list[tuple[ASTNode, bool]]
    ) -> None:
        # Runs AFTER the deferred anonymous flush: every function node's
        # minted qn is now in the span registry. An unregistered span means
        # the node was refused everywhere; emit nothing rather than guess.
        for node, invoked in pending:
            loc = self.function_locations.get(function_span_key(module_qn, node))
            if loc is None or loc.qualified_name not in self.function_registry:
                # No claim, or a STALE claim from before a watch-mode removal
                # purged the registry: writing anything would resurrect a
                # sparse node for a function that no longer exists.
                continue
            if invoked:
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                    cs.RelationshipType.CALLS,
                    (loc.label, cs.KEY_QUALIFIED_NAME, loc.qualified_name),
                )
            else:
                # The module's one export: aliases calling the whole-module
                # require resolve to it, and it is exported by definition
                # (the named form's earlier registration may have recorded
                # is_exported False; the merge fixes it up).
                self.ingestor.ensure_node_batch(
                    loc.label,
                    {
                        cs.KEY_QUALIFIED_NAME: loc.qualified_name,
                        cs.KEY_IS_EXPORTED: True,
                    },
                )
                self.import_processor.commonjs_direct_exports[module_qn] = (
                    loc.qualified_name
                )

    def _ingest_commonjs_exports(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in cs.JS_TS_LANGUAGES:
            return

        language_obj = queries[language].get(cs.QUERY_LANGUAGE)
        if not language_obj:
            return

        # Reset first: an exception in a later pass must not leak a stale
        # pending list into the next file's finalisation.
        self._pending_direct_module_exports = []
        query_texts = [
            cs.JS_COMMONJS_EXPORTS_FUNCTION_QUERY,
            cs.JS_COMMONJS_MODULE_EXPORTS_QUERY,
        ]
        self._pending_direct_module_exports = self._ingest_direct_module_export(
            root_node, module_qn, language_obj
        )

        for query_text in query_texts:
            try:
                cursor = QueryCursor(get_cached_query(language_obj, query_text))
                captures = sorted_captures(cursor, root_node)

                self._process_exports_pattern(
                    captures.get(cs.CAPTURE_EXPORTS_OBJ, []),
                    captures.get(cs.CAPTURE_EXPORT_NAME, []),
                    captures.get(cs.CAPTURE_EXPORT_FUNCTION, []),
                    module_qn,
                )

                self._process_module_exports_pattern(
                    captures.get(cs.CAPTURE_MODULE_OBJ, []),
                    captures.get(cs.CAPTURE_EXPORTS_PROP, []),
                    captures.get(cs.CAPTURE_EXPORT_NAME, []),
                    captures.get(cs.CAPTURE_EXPORT_FUNCTION, []),
                    module_qn,
                )

            except Exception as e:
                logger.debug(ls.JS_COMMONJS_EXPORTS_QUERY_FAILED, error=e)

    def _ingest_es6_exports(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        try:
            lang_query = queries[language][cs.QUERY_LANGUAGE]

            for query_text in [
                cs.JS_ES6_EXPORT_CONST_QUERY,
                cs.JS_ES6_EXPORT_FUNCTION_QUERY,
            ]:
                try:
                    cleaned_query = textwrap.dedent(query_text).strip()
                    query = get_cached_query(lang_query, cleaned_query)
                    cursor = QueryCursor(query)
                    captures = sorted_captures(cursor, root_node)

                    export_names = captures.get(cs.CAPTURE_EXPORT_NAME, [])
                    export_functions = captures.get(cs.CAPTURE_EXPORT_FUNCTION, [])

                    for export_name, export_function in zip(
                        export_names, export_functions
                    ):
                        if export_name.text and export_function:
                            if function_name := safe_decode_text(export_name):
                                self._ingest_export_function(
                                    export_function,
                                    function_name,
                                    module_qn,
                                    cs.JS_EXPORT_TYPE_ES6_FUNCTION,
                                )

                    if not export_names:
                        for export_function in export_functions:
                            if export_function:
                                if name_node := export_function.child_by_field_name(
                                    cs.FIELD_NAME
                                ):
                                    if name_node.text:
                                        if function_name := safe_decode_text(name_node):
                                            self._ingest_export_function(
                                                export_function,
                                                function_name,
                                                module_qn,
                                                cs.JS_EXPORT_TYPE_ES6_FUNCTION_DECL,
                                            )

                except Exception as e:
                    logger.debug(ls.JS_ES6_EXPORTS_QUERY_FAILED, error=e)

        except Exception as e:
            logger.debug(ls.JS_ES6_EXPORTS_DETECT_FAILED, error=e)
