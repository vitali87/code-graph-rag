from __future__ import annotations

import textwrap
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Query, QueryCursor

from ... import constants as cs
from ... import logs as ls
from ...types_defs import ASTNode
from ..utils import (
    ingest_exported_function,
    safe_decode_text,
    safe_decode_with_fallback,
)
from .utils import get_js_ts_language_obj

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import (
        FunctionRegistryTrieProtocol,
        LanguageQueries,
        SimpleNameLookup,
    )
    from ..import_processor import ImportProcessor


class JsTsModuleSystemMixin:
    __slots__ = ("_processed_imports",)
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    import_processor: ImportProcessor
    _processed_imports: set[str]

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    @abstractmethod
    def _is_export_inside_function(self, node: ASTNode) -> bool: ...

    def __init__(self) -> None:
        self._processed_imports = set()

    def _ingest_missing_import_patterns(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        language_obj = get_js_ts_language_obj(language, queries)
        if not language_obj:
            return

        try:
            try:
                query = Query(language_obj, cs.JS_COMMONJS_DESTRUCTURE_QUERY)
                cursor = QueryCursor(query)
                captures = cursor.captures(root_node)

                variable_declarators = captures.get(cs.CAPTURE_VARIABLE_DECLARATOR, [])

                for declarator in variable_declarators:
                    self._process_variable_declarator_for_commonjs(
                        declarator, module_qn
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
        self, child: ASTNode, module_name: str, module_qn: str
    ) -> None:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN:
            if child.text is not None and (name := safe_decode_text(child)):
                self._process_commonjs_import(name, module_name, module_qn)
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
            self._process_commonjs_import(alias_name, module_name, module_qn)

    def _process_variable_declarator_for_commonjs(
        self, declarator: ASTNode, module_qn: str
    ) -> None:
        try:
            module_name = self._extract_require_module_name(declarator)
            if not module_name:
                return

            name_node = declarator.child_by_field_name(cs.FIELD_NAME)
            if not name_node:
                return

            for child in name_node.children:
                self._process_destructured_child(child, module_name, module_qn)

        except Exception as e:
            logger.debug(ls.JS_COMMONJS_VAR_DECLARATOR_FAILED, error=e)

    def _process_commonjs_import(
        self, imported_name: str, module_name: str, module_qn: str
    ) -> None:
        try:
            resolved_source_module = self.import_processor._resolve_js_module_path(
                module_name, module_qn
            )

            import_key = f"{module_qn}->{resolved_source_module}"
            if import_key not in self._processed_imports:
                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.MODULE,
                    {
                        cs.KEY_QUALIFIED_NAME: resolved_source_module,
                        cs.KEY_NAME: resolved_source_module,
                    },
                )

                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                    cs.RelationshipType.IMPORTS,
                    (
                        cs.NodeLabel.MODULE,
                        cs.KEY_QUALIFIED_NAME,
                        resolved_source_module,
                    ),
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
        ingest_exported_function(
            export_function,
            function_name,
            module_qn,
            export_type,
            self.ingestor,
            self.function_registry,
            self.simple_name_lookup,
            self._get_docstring,
            self._is_export_inside_function,
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

    def _ingest_commonjs_exports(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in cs.JS_TS_LANGUAGES:
            return

        language_obj = queries[language].get(cs.QUERY_LANGUAGE)
        if not language_obj:
            return

        query_texts = [
            cs.JS_COMMONJS_EXPORTS_FUNCTION_QUERY,
            cs.JS_COMMONJS_MODULE_EXPORTS_QUERY,
        ]

        for query_text in query_texts:
            try:
                captures = QueryCursor(Query(language_obj, query_text)).captures(
                    root_node
                )

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
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        try:
            lang_query = queries[language][cs.QUERY_LANGUAGE]

            for query_text in [
                cs.JS_ES6_EXPORT_CONST_QUERY,
                cs.JS_ES6_EXPORT_FUNCTION_QUERY,
            ]:
                try:
                    cleaned_query = textwrap.dedent(query_text).strip()
                    query = Query(lang_query, cleaned_query)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    export_names = captures.get(cs.CAPTURE_EXPORT_NAME, [])
                    export_functions = captures.get(cs.CAPTURE_EXPORT_FUNCTION, [])

                    for export_name, export_function in zip(
                        export_names, export_functions
                    ):
                        if export_name.text and export_function:
                            if function_name := safe_decode_text(export_name):
                                ingest_exported_function(
                                    export_function,
                                    function_name,
                                    module_qn,
                                    cs.JS_EXPORT_TYPE_ES6_FUNCTION,
                                    self.ingestor,
                                    self.function_registry,
                                    self.simple_name_lookup,
                                    self._get_docstring,
                                    self._is_export_inside_function,
                                )

                    if not export_names:
                        for export_function in export_functions:
                            if export_function:
                                if name_node := export_function.child_by_field_name(
                                    cs.FIELD_NAME
                                ):
                                    if name_node.text:
                                        if function_name := safe_decode_text(name_node):
                                            ingest_exported_function(
                                                export_function,
                                                function_name,
                                                module_qn,
                                                cs.JS_EXPORT_TYPE_ES6_FUNCTION_DECL,
                                                self.ingestor,
                                                self.function_registry,
                                                self.simple_name_lookup,
                                                self._get_docstring,
                                                self._is_export_inside_function,
                                            )

                except Exception as e:
                    logger.debug(ls.JS_ES6_EXPORTS_QUERY_FAILED, error=e)

        except Exception as e:
            logger.debug(ls.JS_ES6_EXPORTS_DETECT_FAILED, error=e)
