from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ... import logs as lg
from ...types_defs import LanguageQueries
from ..js_ts.utils import find_method_in_ast as find_js_method_in_ast
from ..utils import safe_decode_text

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ..factory import ASTCacheProtocol
    from ..js_ts.type_inference import JsTypeInferenceEngine

    class _AstAnalyzerDeps(Protocol):
        def build_local_variable_type_map(
            self, caller_node: Node, module_qn: str
        ) -> dict[str, str]: ...

        def _analyze_comprehension(
            self, node: Node, local_var_types: dict[str, str], module_qn: str
        ) -> None: ...

        def _analyze_for_loop(
            self, node: Node, local_var_types: dict[str, str], module_qn: str
        ) -> None: ...

        def _infer_instance_variable_types_from_assignments(
            self,
            assignments: list[Node],
            local_var_types: dict[str, str],
            module_qn: str,
        ) -> None: ...

    _AstBase: type = _AstAnalyzerDeps
else:
    _AstBase = object


class PythonAstAnalyzerMixin(_AstBase):
    __slots__ = ()
    queries: dict[cs.SupportedLanguage, LanguageQueries]
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol

    _js_type_inference_getter: Callable[[], JsTypeInferenceEngine]

    @abstractmethod
    def _infer_type_from_expression(self, node: Node, module_qn: str) -> str | None: ...

    @abstractmethod
    def _infer_type_from_expression_simple(
        self, node: Node, module_qn: str
    ) -> str | None: ...

    @abstractmethod
    def _infer_type_from_expression_complex(
        self, node: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> str | None: ...

    @abstractmethod
    def _infer_method_call_return_type(
        self, method_qn: str, module_qn: str, local_var_types: dict[str, str] | None
    ) -> str | None: ...

    @abstractmethod
    def _find_class_in_scope(self, class_name: str, module_qn: str) -> str | None: ...

    def _traverse_single_pass(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        assignments: list[Node] = []
        comprehensions: list[Node] = []
        for_statements: list[Node] = []

        stack: list[Node] = [node]
        while stack:
            current = stack.pop()
            node_type = current.type

            if node_type == cs.TS_PY_ASSIGNMENT:
                assignments.append(current)
            elif node_type == cs.TS_PY_LIST_COMPREHENSION:
                comprehensions.append(current)
            elif node_type == cs.TS_PY_FOR_STATEMENT:
                for_statements.append(current)

            stack.extend(reversed(current.children))

        for assignment in assignments:
            self._process_assignment_simple(assignment, local_var_types, module_qn)

        for assignment in assignments:
            self._process_assignment_complex(assignment, local_var_types, module_qn)

        for comp in comprehensions:
            self._analyze_comprehension(comp, local_var_types, module_qn)

        for for_stmt in for_statements:
            self._analyze_for_loop(for_stmt, local_var_types, module_qn)

        self._infer_instance_variable_types_from_assignments(
            assignments, local_var_types, module_qn
        )

    def _traverse_for_assignments(
        self,
        node: Node,
        local_var_types: dict[str, str],
        module_qn: str,
        processor: Callable[[Node, dict[str, str], str], None],
    ) -> None:
        stack: list[Node] = [node]
        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_ASSIGNMENT:
                processor(current, local_var_types, module_qn)
            stack.extend(reversed(current.children))

    def _process_assignment_simple(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name(cs.TS_FIELD_LEFT)
        right_node = assignment_node.child_by_field_name(cs.TS_FIELD_RIGHT)

        if not left_node or not right_node:
            return

        var_name = self._extract_assignment_variable_name(left_node)
        if not var_name:
            return

        if inferred_type := self._infer_type_from_expression_simple(
            right_node, module_qn
        ):
            local_var_types[var_name] = inferred_type
            logger.debug(lg.PY_TYPE_SIMPLE, var=var_name, type=inferred_type)

    def _process_assignment_complex(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name(cs.TS_FIELD_LEFT)
        right_node = assignment_node.child_by_field_name(cs.TS_FIELD_RIGHT)

        if not left_node or not right_node:
            return

        var_name = self._extract_assignment_variable_name(left_node)
        if not var_name:
            return

        if var_name in local_var_types:
            return

        if inferred_type := self._infer_type_from_expression_complex(
            right_node, module_qn, local_var_types
        ):
            local_var_types[var_name] = inferred_type
            logger.debug(lg.PY_TYPE_COMPLEX, var=var_name, type=inferred_type)

    def _extract_assignment_variable_name(self, node: Node) -> str | None:
        if node.type != cs.TS_PY_IDENTIFIER or node.text is None:
            return None
        return safe_decode_text(node) or None

    def _find_method_ast_node(self, method_qn: str) -> Node | None:
        qn_parts = method_qn.split(cs.SEPARATOR_DOT)
        if len(qn_parts) < 3:
            return None

        class_name = qn_parts[-2]
        method_name = qn_parts[-1]

        expected_module = cs.SEPARATOR_DOT.join(qn_parts[:-2])
        file_path = self.module_qn_to_file_path.get(expected_module)
        if not file_path or file_path not in self.ast_cache:
            return None

        root_node, language = self.ast_cache[file_path]
        return self._find_method_in_ast(root_node, class_name, method_name, language)

    def _find_method_in_ast(
        self,
        root_node: Node,
        class_name: str,
        method_name: str,
        language: cs.SupportedLanguage,
    ) -> Node | None:
        match language:
            case cs.SupportedLanguage.PYTHON:
                return self._find_python_method_in_ast(
                    root_node, class_name, method_name
                )
            case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS:
                return find_js_method_in_ast(root_node, class_name, method_name)
            case _:
                return None

    def _find_python_method_in_ast(
        self, root_node: Node, class_name: str, method_name: str
    ) -> Node | None:
        lang_queries = self.queries[cs.SupportedLanguage.PYTHON]
        class_query = lang_queries[cs.QUERY_KEY_CLASSES]
        if not class_query:
            return None
        cursor = QueryCursor(class_query)
        captures = cursor.captures(root_node)

        method_query = lang_queries[cs.QUERY_KEY_FUNCTIONS]
        if not method_query:
            return None

        for class_node in captures.get(cs.QUERY_CAPTURE_CLASS, []):
            if not isinstance(class_node, Node):
                continue

            name_node = class_node.child_by_field_name(cs.TS_FIELD_NAME)
            if not name_node or name_node.text is None:
                continue

            if safe_decode_text(name_node) != class_name:
                continue

            body_node = class_node.child_by_field_name(cs.TS_FIELD_BODY)
            if not body_node:
                continue

            method_cursor = QueryCursor(method_query)
            method_captures = method_cursor.captures(body_node)

            for method_node in method_captures.get(cs.QUERY_CAPTURE_FUNCTION, []):
                if not isinstance(method_node, Node):
                    continue

                method_name_node = method_node.child_by_field_name(cs.TS_FIELD_NAME)
                if not method_name_node or method_name_node.text is None:
                    continue

                if safe_decode_text(method_name_node) == method_name:
                    return method_node

        return None

    def _analyze_method_return_statements(
        self, method_node: Node, method_qn: str
    ) -> str | None:
        return_nodes: list[Node] = []
        self._find_return_statements(method_node, return_nodes)

        for return_node in return_nodes:
            return_value = next(
                (
                    child
                    for child in return_node.children
                    if child.type not in (cs.TS_PY_RETURN, cs.TS_PY_KEYWORD)
                ),
                None,
            )
            if return_value and (
                inferred_type := self._analyze_return_expression(
                    return_value, method_qn
                )
            ):
                return inferred_type

        return None

    def _find_return_statements(self, node: Node, return_nodes: list[Node]) -> None:
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_RETURN_STATEMENT:
                return_nodes.append(current)

            stack.extend(reversed(current.children))

    def _analyze_return_expression(self, expr_node: Node, method_qn: str) -> str | None:
        match expr_node.type:
            case cs.TS_PY_CALL:
                return self._analyze_call_return(expr_node, method_qn)
            case cs.TS_PY_IDENTIFIER:
                return self._analyze_identifier_return(expr_node, method_qn)
            case cs.TS_PY_ATTRIBUTE:
                return self._analyze_attribute_return(expr_node, method_qn)
            case _:
                return None

    def _analyze_call_return(self, expr_node: Node, method_qn: str) -> str | None:
        func_node = expr_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if not func_node:
            return None

        if (
            func_node.type == cs.TS_PY_IDENTIFIER
            and func_node.text is not None
            and (class_name := safe_decode_text(func_node))
        ):
            return self._resolve_call_class_name(class_name, method_qn)

        if func_node.type == cs.TS_PY_ATTRIBUTE:
            if method_call_text := self._extract_method_call_from_attr(func_node):
                module_qn = cs.SEPARATOR_DOT.join(
                    method_qn.split(cs.SEPARATOR_DOT)[:-2]
                )
                return self._infer_method_call_return_type(
                    method_call_text, module_qn, None
                )

        return None

    def _resolve_call_class_name(self, class_name: str, method_qn: str) -> str | None:
        qn_parts = method_qn.split(cs.SEPARATOR_DOT)
        if class_name == cs.PY_KEYWORD_CLS and len(qn_parts) >= 2:
            return qn_parts[-2]

        if class_name[0].isupper():
            module_qn = cs.SEPARATOR_DOT.join(qn_parts[:-2])
            resolved_class = self._find_class_in_scope(class_name, module_qn)
            return resolved_class or class_name

        return None

    def _analyze_identifier_return(self, expr_node: Node, method_qn: str) -> str | None:
        if expr_node.text is None:
            return None

        identifier = safe_decode_text(expr_node)
        if not identifier:
            return None

        if identifier in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
            qn_parts = method_qn.split(cs.SEPARATOR_DOT)
            return qn_parts[-2] if len(qn_parts) >= 2 else None

        module_qn = cs.SEPARATOR_DOT.join(method_qn.split(cs.SEPARATOR_DOT)[:-2])
        if method_node := self._find_method_ast_node(method_qn):
            local_vars = self.build_local_variable_type_map(method_node, module_qn)
            if identifier in local_vars:
                logger.debug(
                    lg.PY_VAR_FROM_CONTEXT, var=identifier, type=local_vars[identifier]
                )
                return local_vars[identifier]

        logger.debug(lg.PY_VAR_CANNOT_INFER, var=identifier)
        return None

    def _analyze_attribute_return(self, expr_node: Node, method_qn: str) -> str | None:
        object_node = expr_node.child_by_field_name(cs.TS_FIELD_OBJECT)
        if (
            object_node
            and object_node.type == cs.TS_PY_IDENTIFIER
            and object_node.text is not None
            and (object_name := safe_decode_text(object_node))
            and object_name in (cs.PY_KEYWORD_CLS, cs.PY_KEYWORD_SELF)
        ):
            qn_parts = method_qn.split(cs.SEPARATOR_DOT)
            return qn_parts[-2] if len(qn_parts) >= 2 else None

        return None

    def _extract_method_call_from_attr(self, attr_node: Node) -> str | None:
        return safe_decode_text(attr_node) or None if attr_node.text else None
