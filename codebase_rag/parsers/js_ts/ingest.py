from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Query, QueryCursor

from ... import constants as cs
from ... import logs as lg
from ...types_defs import (
    ASTNode,
    FunctionRegistryTrieProtocol,
    NodeType,
    PropertyDict,
    SimpleNameLookup,
)
from ..utils import safe_decode_text, safe_decode_with_fallback
from .module_system import JsTsModuleSystemMixin
from .utils import get_js_ts_language_obj

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...services import IngestorProtocol
    from ...types_defs import LanguageQueries
    from ..import_processor import ImportProcessor


class JsTsIngestMixin(JsTsModuleSystemMixin):
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    class_inheritance: dict[str, list[str]]

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    def _ingest_prototype_inheritance(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in cs.JS_TS_LANGUAGES:
            return

        self._ingest_prototype_inheritance_links(
            root_node, module_qn, language, queries
        )

        self._ingest_prototype_method_assignments(
            root_node, module_qn, language, queries
        )

    def _ingest_prototype_inheritance_links(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]

        language_obj = lang_queries.get(cs.QUERY_LANGUAGE)
        if not language_obj:
            return

        try:
            self._process_prototype_inheritance_captures(
                language_obj, root_node, module_qn
            )
        except Exception as e:
            logger.debug(lg.JS_PROTOTYPE_INHERITANCE_FAILED.format(error=e))

    def _process_prototype_inheritance_captures(
        self, language_obj, root_node, module_qn
    ):
        query = Query(language_obj, cs.JS_PROTOTYPE_INHERITANCE_QUERY)
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)

        child_classes = captures.get(cs.CAPTURE_CHILD_CLASS, [])
        parent_classes = captures.get(cs.CAPTURE_PARENT_CLASS, [])

        if child_classes and parent_classes:
            for child_node, parent_node in zip(child_classes, parent_classes):
                if not child_node.text or not parent_node.text:
                    continue
                child_name = safe_decode_text(child_node)
                parent_name = safe_decode_text(parent_node)

                child_qn = f"{module_qn}{cs.SEPARATOR_DOT}{child_name}"
                parent_qn = f"{module_qn}{cs.SEPARATOR_DOT}{parent_name}"

                if child_qn not in self.class_inheritance:
                    self.class_inheritance[child_qn] = []
                if parent_qn not in self.class_inheritance[child_qn]:
                    self.class_inheritance[child_qn].append(parent_qn)

                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, child_qn),
                    cs.RelationshipType.INHERITS,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, parent_qn),
                )

                logger.debug(
                    lg.JS_PROTOTYPE_INHERITANCE.format(
                        child_qn=child_qn, parent_qn=parent_qn
                    )
                )

    def _ingest_prototype_method_assignments(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]

        language_obj = lang_queries.get(cs.QUERY_LANGUAGE)
        if not language_obj:
            return

        try:
            self._process_prototype_method_captures(language_obj, root_node, module_qn)
        except Exception as e:
            logger.debug(lg.JS_PROTOTYPE_METHODS_FAILED.format(error=e))

    def _process_prototype_method_captures(self, language_obj, root_node, module_qn):
        method_query = Query(language_obj, cs.JS_PROTOTYPE_METHOD_QUERY)
        method_cursor = QueryCursor(method_query)
        method_captures = method_cursor.captures(root_node)

        constructor_names = method_captures.get(cs.CAPTURE_CONSTRUCTOR_NAME, [])
        method_names = method_captures.get(cs.CAPTURE_METHOD_NAME, [])
        method_functions = method_captures.get(cs.CAPTURE_METHOD_FUNCTION, [])

        for constructor_node, method_node, func_node in zip(
            constructor_names, method_names, method_functions
        ):
            constructor_name = (
                safe_decode_text(constructor_node) if constructor_node.text else None
            )
            method_name = safe_decode_text(method_node) if method_node.text else None

            if constructor_name and method_name:
                constructor_qn = f"{module_qn}{cs.SEPARATOR_DOT}{constructor_name}"
                method_qn = f"{constructor_qn}{cs.SEPARATOR_DOT}{method_name}"

                method_props: PropertyDict = {
                    cs.KEY_QUALIFIED_NAME: method_qn,
                    cs.KEY_NAME: method_name,
                    cs.KEY_START_LINE: func_node.start_point[0] + 1,
                    cs.KEY_END_LINE: func_node.end_point[0] + 1,
                    cs.KEY_DOCSTRING: self._get_docstring(func_node),
                }
                logger.info(
                    lg.JS_PROTOTYPE_METHOD_FOUND.format(
                        method_name=method_name, method_qn=method_qn
                    )
                )
                self.ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, method_props)

                self.function_registry[method_qn] = NodeType.FUNCTION
                self.simple_name_lookup[method_name].add(method_qn)

                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, constructor_qn),
                    cs.RelationshipType.DEFINES,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, method_qn),
                )

                logger.debug(
                    lg.JS_PROTOTYPE_METHOD_DEFINES.format(
                        constructor_qn=constructor_qn, method_qn=method_qn
                    )
                )

    def _ingest_object_literal_methods(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        language_obj = get_js_ts_language_obj(language, queries)
        if not language_obj:
            return

        lang_config = queries[language].get(cs.QUERY_CONFIG)
        try:
            for query_text in [cs.JS_OBJECT_METHOD_QUERY, cs.JS_METHOD_DEF_QUERY]:
                self._process_object_method_query(
                    language_obj, query_text, root_node, module_qn, lang_config
                )
        except Exception as e:
            logger.debug(lg.JS_OBJECT_METHODS_DETECT_FAILED.format(error=e))

    def _process_object_method_query(
        self,
        language_obj,
        query_text: str,
        root_node: ASTNode,
        module_qn: str,
        lang_config,
    ) -> None:
        try:
            query = Query(language_obj, query_text)
            cursor = QueryCursor(query)
            captures = cursor.captures(root_node)

            method_names = captures.get(cs.CAPTURE_METHOD_NAME, [])
            method_functions = captures.get(cs.CAPTURE_METHOD_FUNCTION, [])

            for method_name_node, method_func_node in zip(
                method_names, method_functions
            ):
                self._process_single_object_method(
                    method_name_node, method_func_node, module_qn, lang_config
                )
        except Exception as e:
            logger.debug(lg.JS_OBJECT_METHODS_PROCESS_FAILED.format(error=e))

    def _process_single_object_method(
        self,
        method_name_node: ASTNode,
        method_func_node: ASTNode,
        module_qn: str,
        lang_config,
    ) -> None:
        if not method_name_node.text or not method_func_node:
            return

        method_name = safe_decode_text(method_name_node)
        if not method_name:
            return

        if self._is_class_method(
            method_func_node
        ) and not self._is_inside_method_with_object_literals(method_func_node):
            return

        method_qn = self._resolve_object_method_qn(
            method_name_node, method_func_node, module_qn, method_name, lang_config
        )

        self._register_object_method(
            method_name, method_qn, method_func_node, module_qn
        )

    def _resolve_object_method_qn(
        self,
        method_name_node: ASTNode,
        method_func_node: ASTNode,
        module_qn: str,
        method_name: str,
        lang_config,
    ) -> str:
        if lang_config:
            method_qn = self._build_object_method_qualified_name(
                method_name_node, method_func_node, module_qn, method_name, lang_config
            )
            if method_qn is not None:
                return method_qn

        object_name = self._find_object_name_for_method(method_name_node)
        if object_name:
            return f"{module_qn}{cs.SEPARATOR_DOT}{object_name}{cs.SEPARATOR_DOT}{method_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{method_name}"

    def _register_object_method(
        self,
        method_name: str,
        method_qn: str,
        method_func_node: ASTNode,
        module_qn: str,
    ) -> None:
        method_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: method_qn,
            cs.KEY_NAME: method_name,
            cs.KEY_START_LINE: method_func_node.start_point[0] + 1,
            cs.KEY_END_LINE: method_func_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: self._get_docstring(method_func_node),
        }
        logger.info(
            lg.JS_OBJECT_METHOD_FOUND.format(
                method_name=method_name, method_qn=method_qn
            )
        )
        self.ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, method_props)

        self.function_registry[method_qn] = NodeType.FUNCTION
        self.simple_name_lookup[method_name].add(method_qn)

        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            cs.RelationshipType.DEFINES,
            (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, method_qn),
        )

    def _ingest_assignment_arrow_functions(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in cs.JS_TS_LANGUAGES:
            return

        try:
            lang_query = queries[language][cs.QUERY_LANGUAGE]
            lang_config = queries[language].get(cs.QUERY_CONFIG)

            for query_text in [
                cs.JS_OBJECT_ARROW_QUERY,
                cs.JS_ASSIGNMENT_ARROW_QUERY,
                cs.JS_ASSIGNMENT_FUNCTION_QUERY,
            ]:
                self._process_arrow_query(
                    lang_query, query_text, root_node, module_qn, lang_config
                )
        except Exception as e:
            logger.debug(lg.JS_ASSIGNMENT_ARROW_DETECT_FAILED.format(error=e))

    def _process_arrow_query(
        self,
        lang_query,
        query_text: str,
        root_node: ASTNode,
        module_qn: str,
        lang_config,
    ) -> None:
        try:
            query = Query(lang_query, query_text)
            cursor = QueryCursor(query)
            captures = cursor.captures(root_node)

            method_names = captures.get(cs.CAPTURE_METHOD_NAME, [])
            member_exprs = captures.get(cs.CAPTURE_MEMBER_EXPR, [])
            arrow_functions = captures.get(cs.CAPTURE_ARROW_FUNCTION, [])
            function_exprs = captures.get(cs.CAPTURE_FUNCTION_EXPR, [])

            self._process_direct_arrow_functions(
                method_names, arrow_functions, module_qn, lang_config
            )
            self._process_member_expr_functions(
                member_exprs,
                arrow_functions,
                module_qn,
                lang_config,
                lg.JS_ASSIGNMENT_ARROW_FOUND,
            )
            self._process_member_expr_functions(
                member_exprs,
                function_exprs,
                module_qn,
                lang_config,
                lg.JS_ASSIGNMENT_FUNC_EXPR_FOUND,
            )
        except Exception as e:
            logger.debug(lg.JS_ASSIGNMENT_ARROW_QUERY_FAILED.format(error=e))

    def _process_direct_arrow_functions(
        self,
        method_names: list[ASTNode],
        arrow_functions: list[ASTNode],
        module_qn: str,
        lang_config,
    ) -> None:
        for method_name, arrow_function in zip(method_names, arrow_functions):
            if not method_name.text or not arrow_function:
                continue

            function_name = safe_decode_text(method_name)
            if not function_name:
                continue

            function_qn = self._resolve_direct_arrow_qn(
                arrow_function, module_qn, function_name, lang_config
            )

            self._register_arrow_function(
                function_name, function_qn, arrow_function, lg.JS_OBJECT_ARROW_FOUND
            )

    def _resolve_direct_arrow_qn(
        self, arrow_function: ASTNode, module_qn: str, function_name: str, lang_config
    ) -> str:
        if lang_config:
            function_qn = self._build_nested_qualified_name(
                arrow_function,
                module_qn,
                function_name,
                lang_config,
                skip_classes=False,
            )
            if function_qn is not None:
                return function_qn
        return f"{module_qn}{cs.SEPARATOR_DOT}{function_name}"

    def _process_member_expr_functions(
        self,
        member_exprs: list[ASTNode],
        function_nodes: list[ASTNode],
        module_qn: str,
        lang_config,
        log_message: str,
    ) -> None:
        for member_expr, function_node in zip(member_exprs, function_nodes):
            if not member_expr.text or not function_node:
                continue

            member_text = safe_decode_with_fallback(member_expr)
            if cs.SEPARATOR_DOT not in member_text:
                continue

            function_name = member_text.split(cs.SEPARATOR_DOT)[-1]
            function_qn = self._resolve_member_expr_qn(
                member_expr, function_node, module_qn, function_name, lang_config
            )

            self._register_arrow_function(
                function_name, function_qn, function_node, log_message
            )

    def _resolve_member_expr_qn(
        self,
        member_expr: ASTNode,
        function_node: ASTNode,
        module_qn: str,
        function_name: str,
        lang_config,
    ) -> str:
        if lang_config:
            function_qn = self._build_assignment_arrow_function_qualified_name(
                member_expr, function_node, module_qn, function_name, lang_config
            )
            if function_qn is not None:
                return function_qn
        return f"{module_qn}{cs.SEPARATOR_DOT}{function_name}"

    def _register_arrow_function(
        self,
        function_name: str,
        function_qn: str,
        function_node: ASTNode,
        log_message: str,
    ) -> None:
        function_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: function_qn,
            cs.KEY_NAME: function_name,
            cs.KEY_START_LINE: function_node.start_point[0] + 1,
            cs.KEY_END_LINE: function_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: self._get_docstring(function_node),
        }

        logger.debug(
            log_message.format(function_name=function_name, function_qn=function_qn)
        )
        self.ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, function_props)
        self.function_registry[function_qn] = NodeType.FUNCTION
        self.simple_name_lookup[function_name].add(function_qn)

    def _is_static_method_in_class(self, method_node: ASTNode) -> bool:
        if method_node.type == cs.TS_METHOD_DEFINITION:
            parent = method_node.parent
            if parent and parent.type == cs.TS_CLASS_BODY:
                for child in method_node.children:
                    if child.type == cs.TS_STATIC:
                        return True
        return False

    def _is_method_in_class(self, method_node: ASTNode) -> bool:
        current = method_node.parent
        while current:
            if current.type == cs.TS_CLASS_BODY:
                return True
            current = current.parent
        return False

    def _is_inside_method_with_object_literals(self, func_node: ASTNode) -> bool:
        current = func_node.parent
        found_object = False

        while current:
            if current.type == cs.TS_OBJECT:
                found_object = True
            elif current.type == cs.TS_METHOD_DEFINITION and found_object:
                return True
            elif current.type == cs.TS_CLASS_BODY:
                break
            current = current.parent

        return False

    def _is_class_method(self, method_node: ASTNode) -> bool:
        current = method_node.parent
        while current:
            if current.type == cs.TS_CLASS_BODY:
                return True
            if current.type in (cs.TS_PROGRAM, cs.TS_MODULE):
                return False
            current = current.parent
        return False

    def _is_export_inside_function(self, export_node: ASTNode) -> bool:
        current = export_node.parent
        while current:
            if current.type in (
                cs.TS_FUNCTION_DECLARATION,
                cs.TS_FUNCTION_EXPRESSION,
                cs.TS_ARROW_FUNCTION,
                cs.TS_METHOD_DEFINITION,
            ):
                return True
            if current.type in (cs.TS_PROGRAM, cs.TS_MODULE):
                return False
            current = current.parent
        return False

    def _find_object_name_for_method(self, method_name_node: ASTNode) -> str | None:
        current = method_name_node.parent
        while current:
            if current.type == cs.TS_VARIABLE_DECLARATOR:
                name_node = current.child_by_field_name(cs.FIELD_NAME)
                if name_node and name_node.type == cs.TS_IDENTIFIER and name_node.text:
                    return str(safe_decode_text(name_node))
            elif current.type == cs.TS_ASSIGNMENT_EXPRESSION:
                left_child = current.child_by_field_name(cs.FIELD_LEFT)
                if (
                    left_child
                    and left_child.type == cs.TS_IDENTIFIER
                    and left_child.text
                ):
                    return str(safe_decode_text(left_child))
            current = current.parent
        return None

    def _build_object_method_qualified_name(
        self,
        method_name_node: ASTNode,
        method_func_node: ASTNode,
        module_qn: str,
        method_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        skip_types = (
            cs.TS_OBJECT,
            cs.TS_VARIABLE_DECLARATOR,
            cs.TS_LEXICAL_DECLARATION,
            cs.TS_ASSIGNMENT_EXPRESSION,
            cs.TS_PAIR,
        )
        path_parts = self._js_collect_ancestor_path_parts(
            method_name_node.parent, lang_config, skip_types
        )
        return self._js_format_qualified_name(module_qn, path_parts, method_name)

    def _build_assignment_arrow_function_qualified_name(
        self,
        member_expr: ASTNode,
        arrow_function: ASTNode,
        module_qn: str,
        function_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        current = member_expr.parent
        if current and current.type == cs.TS_ASSIGNMENT_EXPRESSION:
            current = current.parent

        skip_types = (cs.TS_EXPRESSION_STATEMENT, cs.TS_STATEMENT_BLOCK)
        path_parts = self._js_collect_ancestor_path_parts(
            current, lang_config, skip_types
        )
        return self._js_format_qualified_name(module_qn, path_parts, function_name)

    def _js_collect_ancestor_path_parts(
        self,
        start_node: ASTNode | None,
        lang_config: LanguageSpec,
        skip_types: tuple[str, ...],
    ) -> list[str]:
        path_parts: list[str] = []
        current = start_node

        while current and current.type not in lang_config.module_node_types:
            if current.type in skip_types:
                current = current.parent
                continue

            if name := self._js_extract_ancestor_name(current, lang_config):
                path_parts.append(name)

            current = current.parent

        path_parts.reverse()
        return path_parts

    def _js_extract_ancestor_name(
        self, node: ASTNode, lang_config: LanguageSpec
    ) -> str | None:
        naming_types = (
            *lang_config.function_node_types,
            *lang_config.class_node_types,
            cs.TS_METHOD_DEFINITION,
        )
        if node.type not in naming_types:
            return None

        name_node = node.child_by_field_name(cs.FIELD_NAME)
        return safe_decode_text(name_node) if name_node and name_node.text else None

    def _js_format_qualified_name(
        self, module_qn: str, path_parts: list[str], final_name: str
    ) -> str:
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{final_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{final_name}"
