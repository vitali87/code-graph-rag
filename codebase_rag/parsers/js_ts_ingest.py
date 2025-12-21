from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node, Query, QueryCursor

from ..constants import SEPARATOR_DOT, SupportedLanguage
from ..types_defs import NodeType
from .utils import (
    ingest_exported_function,
    safe_decode_text,
    safe_decode_with_fallback,
)

if TYPE_CHECKING:
    from ..language_spec import LanguageSpec
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .import_processor import ImportProcessor

_JS_TYPESCRIPT_LANGUAGES = {SupportedLanguage.JS, SupportedLanguage.TS}


class JsTsIngestMixin:
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: Any
    simple_name_lookup: Any
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    class_inheritance: dict[str, list[str]]
    _get_docstring: Callable[[Node], str | None]
    _build_nested_qualified_name: Callable[..., str | None]

    def _ingest_prototype_inheritance(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        self._ingest_prototype_inheritance_links(
            root_node, module_qn, language, queries
        )

        self._ingest_prototype_method_assignments(
            root_node, module_qn, language, queries
        )

    def _ingest_prototype_inheritance_links(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]

        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        query_text = """
        (assignment_expression
          left: (member_expression
            object: (identifier) @child_class
            property: (property_identifier) @prototype (#eq? @prototype "prototype"))
          right: (call_expression
            function: (member_expression
              object: (identifier) @object_name (#eq? @object_name "Object")
              property: (property_identifier) @create_method (#eq? @create_method "create"))
            arguments: (arguments
              (member_expression
                object: (identifier) @parent_class
                property: (property_identifier) @parent_prototype (#eq? @parent_prototype "prototype")))))
        """

        try:
            query = Query(language_obj, query_text)
            cursor = QueryCursor(query)
            captures = cursor.captures(root_node)

            child_classes = captures.get("child_class", [])
            parent_classes = captures.get("parent_class", [])

            if child_classes and parent_classes:
                for child_node, parent_node in zip(child_classes, parent_classes):
                    if not child_node.text or not parent_node.text:
                        continue
                    child_name = safe_decode_text(child_node)
                    parent_name = safe_decode_text(parent_node)

                    child_qn = f"{module_qn}.{child_name}"
                    parent_qn = f"{module_qn}.{parent_name}"

                    if child_qn not in self.class_inheritance:
                        self.class_inheritance[child_qn] = []
                    if parent_qn not in self.class_inheritance[child_qn]:
                        self.class_inheritance[child_qn].append(parent_qn)

                    self.ingestor.ensure_relationship_batch(
                        ("Function", "qualified_name", child_qn),
                        "INHERITS",
                        ("Function", "qualified_name", parent_qn),
                    )

                    logger.debug(
                        f"Prototype inheritance: {child_qn} INHERITS {parent_qn}"
                    )

        except Exception as e:
            logger.debug(f"Failed to detect prototype inheritance: {e}")

    def _ingest_prototype_method_assignments(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]

        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        prototype_method_query = """
        (assignment_expression
          left: (member_expression
            object: (member_expression
              object: (identifier) @constructor_name
              property: (property_identifier) @prototype_keyword (#eq? @prototype_keyword "prototype"))
            property: (property_identifier) @method_name)
          right: (function_expression) @method_function)
        """

        try:
            method_query = Query(language_obj, prototype_method_query)
            method_cursor = QueryCursor(method_query)
            method_captures = method_cursor.captures(root_node)

            constructor_names = method_captures.get("constructor_name", [])
            method_names = method_captures.get("method_name", [])
            method_functions = method_captures.get("method_function", [])

            for constructor_node, method_node, func_node in zip(
                constructor_names, method_names, method_functions
            ):
                constructor_name = (
                    safe_decode_text(constructor_node)
                    if constructor_node.text
                    else None
                )
                method_name = (
                    safe_decode_text(method_node) if method_node.text else None
                )

                if constructor_name and method_name:
                    constructor_qn = f"{module_qn}.{constructor_name}"
                    method_qn = f"{constructor_qn}.{method_name}"

                    method_props = {
                        "qualified_name": method_qn,
                        "name": method_name,
                        "start_line": func_node.start_point[0] + 1,
                        "end_line": func_node.end_point[0] + 1,
                        "docstring": self._get_docstring(func_node),
                    }
                    logger.info(
                        f"  Found Prototype Method: {method_name} (qn: {method_qn})"
                    )
                    self.ingestor.ensure_node_batch("Function", method_props)

                    self.function_registry[method_qn] = NodeType.FUNCTION
                    self.simple_name_lookup[method_name].add(method_qn)

                    self.ingestor.ensure_relationship_batch(
                        ("Function", "qualified_name", constructor_qn),
                        "DEFINES",
                        ("Function", "qualified_name", method_qn),
                    )

                    logger.debug(
                        f"Prototype method: {constructor_qn} DEFINES {method_qn}"
                    )

        except Exception as e:
            logger.debug(f"Failed to detect prototype methods: {e}")

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

    def _ingest_object_literal_methods(
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
            object_method_query = """
            (pair
              key: (property_identifier) @method_name
              value: (function_expression) @method_function)
            """

            method_def_query = """
            (object
              (method_definition
                name: (property_identifier) @method_name) @method_function)
            """

            for query_text in [object_method_query, method_def_query]:
                try:
                    query = Query(language_obj, query_text)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    method_names = captures.get("method_name", [])
                    method_functions = captures.get("method_function", [])

                    for method_name_node, method_func_node in zip(
                        method_names, method_functions
                    ):
                        if method_name_node.text and method_func_node:
                            method_name = safe_decode_text(method_name_node)

                            if self._is_class_method(
                                method_func_node
                            ) and not self._is_inside_method_with_object_literals(
                                method_func_node
                            ):
                                continue

                            lang_config = lang_queries.get("config")
                            if lang_config and method_name:
                                method_qn = self._build_object_method_qualified_name(
                                    method_name_node,
                                    method_func_node,
                                    module_qn,
                                    method_name,
                                    lang_config,
                                )
                                if method_qn is None:
                                    method_qn = f"{module_qn}.{method_name}"
                            else:
                                object_name = self._find_object_name_for_method(
                                    method_name_node
                                )
                                if object_name:
                                    method_qn = (
                                        f"{module_qn}.{object_name}.{method_name}"
                                    )
                                else:
                                    method_qn = f"{module_qn}.{method_name}"

                            method_props: dict[str, Any] = {
                                "qualified_name": method_qn,
                                "name": method_name,
                                "start_line": method_func_node.start_point[0] + 1,
                                "end_line": method_func_node.end_point[0] + 1,
                                "docstring": self._get_docstring(method_func_node),
                            }
                            logger.info(
                                f"  Found Object Method: {method_name} (qn: {method_qn})"
                            )
                            self.ingestor.ensure_node_batch("Function", method_props)

                            self.function_registry[method_qn] = NodeType.FUNCTION
                            if method_name:
                                self.simple_name_lookup[method_name].add(method_qn)

                            self.ingestor.ensure_relationship_batch(
                                ("Module", "qualified_name", module_qn),
                                "DEFINES",
                                ("Function", "qualified_name", method_qn),
                            )

                except Exception as e:
                    logger.debug(f"Failed to process object literal methods: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect object literal methods: {e}")

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

    def _ingest_assignment_arrow_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        try:
            lang_query = queries[language]["language"]

            object_arrow_query = """
            (object
              (pair
                (property_identifier) @method_name
                (arrow_function) @arrow_function))
            """

            assignment_arrow_query = """
            (assignment_expression
              (member_expression) @member_expr
              (arrow_function) @arrow_function)
            """

            assignment_function_query = """
            (assignment_expression
              (member_expression) @member_expr
              (function_expression) @function_expr)
            """

            for query_text in [
                object_arrow_query,
                assignment_arrow_query,
                assignment_function_query,
            ]:
                try:
                    query = Query(lang_query, query_text)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    method_names = captures.get("method_name", [])
                    member_exprs = captures.get("member_expr", [])
                    arrow_functions = captures.get("arrow_function", [])
                    function_exprs = captures.get("function_expr", [])

                    for method_name, arrow_function in zip(
                        method_names, arrow_functions
                    ):
                        if method_name.text and arrow_function:
                            function_name = safe_decode_text(method_name)

                            lang_config = queries[language].get("config")
                            if lang_config and function_name:
                                function_qn = self._build_nested_qualified_name(
                                    arrow_function,
                                    module_qn,
                                    function_name,
                                    lang_config,
                                    skip_classes=False,
                                )
                                if function_qn is None:
                                    function_qn = f"{module_qn}.{function_name}"
                            else:
                                function_qn = f"{module_qn}.{function_name}"

                            function_props: dict[str, Any] = {
                                "qualified_name": function_qn,
                                "name": function_name,
                                "start_line": arrow_function.start_point[0] + 1,
                                "end_line": arrow_function.end_point[0] + 1,
                                "docstring": self._get_docstring(arrow_function),
                            }

                            logger.debug(
                                f"  Found Object Arrow Function: {function_name} (qn: {function_qn})"
                            )
                            self.ingestor.ensure_node_batch("Function", function_props)
                            self.function_registry[function_qn] = NodeType.FUNCTION
                            if function_name:
                                self.simple_name_lookup[function_name].add(function_qn)

                    for member_expr, arrow_function in zip(
                        member_exprs, arrow_functions
                    ):
                        if member_expr.text and arrow_function:
                            member_text = safe_decode_with_fallback(member_expr)
                            if SEPARATOR_DOT in member_text:
                                function_name = member_text.split(SEPARATOR_DOT)[-1]

                                if lang_config := queries[language].get("config"):
                                    function_qn = self._build_assignment_arrow_function_qualified_name(
                                        member_expr,
                                        arrow_function,
                                        module_qn,
                                        function_name,
                                        lang_config,
                                    )
                                    if function_qn is None:
                                        function_qn = f"{module_qn}.{function_name}"
                                else:
                                    function_qn = f"{module_qn}.{function_name}"

                                function_props = {
                                    "qualified_name": function_qn,
                                    "name": function_name,
                                    "start_line": arrow_function.start_point[0] + 1,
                                    "end_line": arrow_function.end_point[0] + 1,
                                    "docstring": self._get_docstring(arrow_function),
                                }

                                logger.debug(
                                    f"  Found Assignment Arrow Function: {function_name} (qn: {function_qn})"
                                )
                                self.ingestor.ensure_node_batch(
                                    "Function", function_props
                                )
                                self.function_registry[function_qn] = NodeType.FUNCTION
                                self.simple_name_lookup[function_name].add(function_qn)

                    for member_expr, function_expr in zip(member_exprs, function_exprs):
                        if member_expr.text and function_expr:
                            member_text = safe_decode_with_fallback(member_expr)
                            if SEPARATOR_DOT in member_text:
                                function_name = member_text.split(SEPARATOR_DOT)[-1]

                                if lang_config := queries[language].get("config"):
                                    function_qn = self._build_assignment_arrow_function_qualified_name(
                                        member_expr,
                                        function_expr,
                                        module_qn,
                                        function_name,
                                        lang_config,
                                    )
                                    if function_qn is None:
                                        function_qn = f"{module_qn}.{function_name}"
                                else:
                                    function_qn = f"{module_qn}.{function_name}"

                                function_props = {
                                    "qualified_name": function_qn,
                                    "name": function_name,
                                    "start_line": function_expr.start_point[0] + 1,
                                    "end_line": function_expr.end_point[0] + 1,
                                    "docstring": self._get_docstring(function_expr),
                                }

                                logger.debug(
                                    f"  Found Assignment Function Expression: {function_name} (qn: {function_qn})"
                                )
                                self.ingestor.ensure_node_batch(
                                    "Function", function_props
                                )
                                self.function_registry[function_qn] = NodeType.FUNCTION
                                self.simple_name_lookup[function_name].add(function_qn)

                except Exception as e:
                    logger.debug(
                        f"Failed to process assignment arrow functions query: {e}"
                    )

        except Exception as e:
            logger.debug(f"Failed to detect assignment arrow functions: {e}")

    def _is_static_method_in_class(self, method_node: Node) -> bool:
        if method_node.type == "method_definition":
            parent = method_node.parent
            if parent and parent.type == "class_body":
                for child in method_node.children:
                    if child.type == "static":
                        return True
        return False

    def _is_method_in_class(self, method_node: Node) -> bool:
        current = method_node.parent
        while current:
            if current.type == "class_body":
                return True
            current = current.parent
        return False

    def _is_inside_method_with_object_literals(self, func_node: Node) -> bool:
        current = func_node.parent
        found_object = False

        while current:
            if current.type == "object":
                found_object = True
            elif current.type == "method_definition" and found_object:
                return True
            elif current.type == "class_body":
                break
            current = current.parent

        return False

    def _is_class_method(self, method_node: Node) -> bool:
        current = method_node.parent
        while current:
            if current.type == "class_body":
                return True
            elif current.type in ["program", "module"]:
                return False
            current = current.parent
        return False

    def _is_export_inside_function(self, export_node: Node) -> bool:
        current = export_node.parent
        while current:
            if current.type in [
                "function_declaration",
                "function_expression",
                "arrow_function",
                "method_definition",
            ]:
                return True
            elif current.type in ["program", "module"]:
                return False
            current = current.parent
        return False

    def _find_object_name_for_method(self, method_name_node: Node) -> str | None:
        current = method_name_node.parent
        while current:
            if current.type == "variable_declarator":
                name_node = current.child_by_field_name("name")
                if name_node and name_node.type == "identifier" and name_node.text:
                    return str(safe_decode_text(name_node))
            elif current.type == "assignment_expression":
                left_child = current.child_by_field_name("left")
                if left_child and left_child.type == "identifier" and left_child.text:
                    return str(safe_decode_text(left_child))
            current = current.parent
        return None

    def _build_object_method_qualified_name(
        self,
        method_name_node: Node,
        method_func_node: Node,
        module_qn: str,
        method_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts: list[str] = []

        current = method_name_node.parent

        while current and current.type not in lang_config.module_node_types:
            if current.type in [
                "object",
                "variable_declarator",
                "variable_declaration",
                "assignment_expression",
                "pair",
            ]:
                current = current.parent
                continue

            if current.type in lang_config.function_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    if decoded := safe_decode_text(name_node):
                        path_parts.append(decoded)
            elif current.type in lang_config.class_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    if decoded := safe_decode_text(name_node):
                        path_parts.append(decoded)
            elif current.type == "method_definition":
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    if decoded := safe_decode_text(name_node):
                        path_parts.append(decoded)

            current = current.parent

        path_parts.reverse()

        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{method_name}"
        return f"{module_qn}.{method_name}"

    def _build_assignment_arrow_function_qualified_name(
        self,
        member_expr: Node,
        arrow_function: Node,
        module_qn: str,
        function_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts: list[str] = []

        current = member_expr.parent
        if current and current.type == "assignment_expression":
            current = current.parent

        while current and current.type not in lang_config.module_node_types:
            if current.type in ["expression_statement", "statement_block"]:
                current = current.parent
                continue

            if current.type in lang_config.function_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    if decoded := safe_decode_text(name_node):
                        path_parts.append(decoded)
            elif current.type in lang_config.class_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    if decoded := safe_decode_text(name_node):
                        path_parts.append(decoded)
            elif current.type == "method_definition":
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    if decoded := safe_decode_text(name_node):
                        path_parts.append(decoded)

            current = current.parent

        path_parts.reverse()

        if path_parts:
            return f"{module_qn}.{SEPARATOR_DOT.join(path_parts)}.{function_name}"
        return f"{module_qn}.{function_name}"
