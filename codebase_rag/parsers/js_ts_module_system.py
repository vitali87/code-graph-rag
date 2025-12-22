from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node, Query, QueryCursor

from ..constants import SupportedLanguage
from .utils import (
    ingest_exported_function,
    safe_decode_text,
    safe_decode_with_fallback,
)

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .import_processor import ImportProcessor

_JS_TYPESCRIPT_LANGUAGES = {SupportedLanguage.JS, SupportedLanguage.TS}


class JsTsModuleSystemMixin:
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: Any
    simple_name_lookup: Any
    import_processor: ImportProcessor
    _get_docstring: Callable[[Node], str | None]
    _is_export_inside_function: Callable[[Node], bool]

    def _ingest_missing_import_patterns(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        lang_queries = queries[language]

        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        try:
            commonjs_destructure_query = """
            (lexical_declaration
              (variable_declarator
                name: (object_pattern)
                value: (call_expression
                  function: (identifier) @func (#eq? @func "require")
                )
              ) @variable_declarator
            )
            """

            try:
                query = Query(language_obj, commonjs_destructure_query)
                cursor = QueryCursor(query)
                captures = cursor.captures(root_node)

                variable_declarators = captures.get("variable_declarator", [])

                for declarator in variable_declarators:
                    self._process_variable_declarator_for_commonjs(
                        declarator, module_qn
                    )

            except Exception as e:
                logger.debug(f"Failed to process CommonJS destructuring pattern: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect missing import patterns: {e}")

    def _process_variable_declarator_for_commonjs(
        self, declarator: Node, module_qn: str
    ) -> None:
        try:
            name_node = declarator.child_by_field_name("name")
            if not name_node or name_node.type != "object_pattern":
                return

            value_node = declarator.child_by_field_name("value")
            if not value_node or value_node.type != "call_expression":
                return

            function_node = value_node.child_by_field_name("function")
            if not function_node or function_node.type != "identifier":
                return

            if (
                function_node.text is None
                or safe_decode_text(function_node) != "require"
            ):
                return

            arguments_node = value_node.child_by_field_name("arguments")
            if not arguments_node or not arguments_node.children:
                return

            module_string_node = next(
                (child for child in arguments_node.children if child.type == "string"),
                None,
            )
            if not module_string_node or module_string_node.text is None:
                return

            module_name = safe_decode_with_fallback(module_string_node).strip("'\"")

            for child in name_node.children:
                if child.type == "shorthand_property_identifier_pattern":
                    if child.text is not None:
                        if destructured_name := safe_decode_text(child):
                            self._process_commonjs_import(
                                destructured_name, module_name, module_qn
                            )

                elif child.type == "pair_pattern":
                    key_node = child.child_by_field_name("key")
                    value_node = child.child_by_field_name("value")

                    if (
                        key_node
                        and key_node.type == "property_identifier"
                        and value_node
                        and value_node.type == "identifier"
                    ) and value_node.text is not None:
                        if alias_name := safe_decode_text(value_node):
                            self._process_commonjs_import(
                                alias_name, module_name, module_qn
                            )

        except Exception as e:
            logger.debug(f"Failed to process variable declarator for CommonJS: {e}")

    def _process_commonjs_import(
        self, imported_name: str, module_name: str, module_qn: str
    ) -> None:
        try:
            resolved_source_module = self.import_processor._resolve_js_module_path(
                module_name, module_qn
            )

            import_key = f"{module_qn}->{resolved_source_module}"
            if import_key not in getattr(self, "_processed_imports", set()):
                self.ingestor.ensure_node_batch(
                    "Module",
                    {
                        "qualified_name": resolved_source_module,
                        "name": resolved_source_module,
                    },
                )

                self.ingestor.ensure_relationship_batch(
                    ("Module", "qualified_name", module_qn),
                    "IMPORTS",
                    (
                        "Module",
                        "qualified_name",
                        resolved_source_module,
                    ),
                )

                logger.debug(
                    f"Missing pattern: {module_qn} IMPORTS {imported_name} from {resolved_source_module}"
                )

                if not hasattr(self, "_processed_imports"):
                    self._processed_imports: set[str] = set()
                self._processed_imports.add(import_key)

        except Exception as e:
            logger.debug(f"Failed to process CommonJS import {imported_name}: {e}")

    def _ingest_commonjs_exports(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        lang_queries = queries[language]
        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        try:
            exports_function_query = """
            (assignment_expression
              left: (member_expression
                object: (identifier) @exports_obj
                property: (property_identifier) @export_name)
              right: [(function_expression) (arrow_function)] @export_function)
            """

            module_exports_query = """
            (assignment_expression
              left: (member_expression
                object: (member_expression
                  object: (identifier) @module_obj
                  property: (property_identifier) @exports_prop)
                property: (property_identifier) @export_name)
              right: [(function_expression) (arrow_function)] @export_function)
            """

            for query_text in [exports_function_query, module_exports_query]:
                try:
                    query = Query(language_obj, query_text)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    exports_objs = captures.get("exports_obj", [])
                    module_objs = captures.get("module_obj", [])
                    exports_props = captures.get("exports_prop", [])
                    export_names = captures.get("export_name", [])
                    export_functions = captures.get("export_function", [])

                    for exports_obj, export_name, export_function in zip(
                        exports_objs, export_names, export_functions
                    ):
                        if (
                            exports_obj.text
                            and export_name.text
                            and safe_decode_text(exports_obj) == "exports"
                        ):
                            if function_name := safe_decode_text(export_name):
                                ingest_exported_function(
                                    export_function,
                                    function_name,
                                    module_qn,
                                    "CommonJS Export",
                                    self.ingestor,
                                    self.function_registry,
                                    self.simple_name_lookup,
                                    self._get_docstring,
                                    self._is_export_inside_function,
                                )

                    for (
                        module_obj,
                        exports_prop,
                        export_name,
                        export_function,
                    ) in zip(
                        module_objs, exports_props, export_names, export_functions
                    ):
                        if (
                            module_obj.text
                            and exports_prop.text
                            and export_name.text
                            and safe_decode_text(module_obj) == "module"
                            and safe_decode_text(exports_prop) == "exports"
                        ):
                            if function_name := safe_decode_text(export_name):
                                ingest_exported_function(
                                    export_function,
                                    function_name,
                                    module_qn,
                                    "CommonJS Module Export",
                                    self.ingestor,
                                    self.function_registry,
                                    self.simple_name_lookup,
                                    self._get_docstring,
                                    self._is_export_inside_function,
                                )

                except Exception as e:
                    logger.debug(f"Failed to process CommonJS exports query: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect CommonJS exports: {e}")

    def _ingest_es6_exports(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        try:
            lang_query = queries[language]["language"]

            export_const_query = """
            (export_statement
              (lexical_declaration
                (variable_declarator
                  name: (identifier) @export_name
                  value: [(function_expression) (arrow_function)] @export_function)))
            """

            export_function_query = """
            (export_statement
              [(function_declaration) (generator_function_declaration)] @export_function)
            """

            for query_text in [export_const_query, export_function_query]:
                try:
                    cleaned_query = textwrap.dedent(query_text).strip()
                    query = Query(lang_query, cleaned_query)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    export_names = captures.get("export_name", [])
                    export_functions = captures.get("export_function", [])

                    for export_name, export_function in zip(
                        export_names, export_functions
                    ):
                        if export_name.text and export_function:
                            if function_name := safe_decode_text(export_name):
                                ingest_exported_function(
                                    export_function,
                                    function_name,
                                    module_qn,
                                    "ES6 Export Function",
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
                                    "name"
                                ):
                                    if name_node.text:
                                        if function_name := safe_decode_text(name_node):
                                            ingest_exported_function(
                                                export_function,
                                                function_name,
                                                module_qn,
                                                "ES6 Export Function Declaration",
                                                self.ingestor,
                                                self.function_registry,
                                                self.simple_name_lookup,
                                                self._get_docstring,
                                                self._is_export_inside_function,
                                            )

                except Exception as e:
                    logger.debug(f"Failed to process ES6 exports query: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect ES6 exports: {e}")
