from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..utils import safe_decode_text
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import ASTNode


def _enclosing_declarator_name(node: ASTNode) -> str | None:
    # The first identifier of the nearest `variable_declarator` ancestor that
    # has one names an arrow function (`const f = () => ...`).
    current = node.parent
    while current:
        if current.type == cs.TS_VARIABLE_DECLARATOR:
            for child in current.children:
                if child.type == cs.TS_IDENTIFIER and child.text:
                    return safe_decode_text(child)
        current = current.parent
    return None


class JsTsHandler(BaseLanguageHandler):
    __slots__ = ()

    def extract_decorators(self, node: ASTNode) -> list[str]:
        return [
            decorator_text
            for child in node.children
            if child.type == cs.TS_DECORATOR
            if (decorator_text := safe_decode_text(child))
        ]

    def is_inside_method_with_object_literals(self, node: ASTNode) -> bool:
        current = node.parent
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

    def is_class_method(self, node: ASTNode) -> bool:
        current = node.parent
        while current:
            if current.type == cs.TS_CLASS_BODY:
                return True
            if current.type in (cs.TS_PROGRAM, cs.TS_MODULE):
                return False
            current = current.parent
        return False

    def is_export_inside_function(self, node: ASTNode) -> bool:
        current = node.parent
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

    def extract_function_name(self, node: ASTNode) -> str | None:
        if (name_node := node.child_by_field_name(cs.TS_FIELD_NAME)) and name_node.text:
            return safe_decode_text(name_node)

        if node.type == cs.TS_ARROW_FUNCTION:
            return _enclosing_declarator_name(node)

        return None

    def build_nested_function_qn(
        self,
        func_node: ASTNode,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts = self._collect_js_ancestor_path_parts(func_node, lang_config)
        if path_parts is None:
            return None
        return self._format_nested_qn(module_qn, path_parts, func_name)

    def _collect_js_ancestor_path_parts(
        self,
        func_node: ASTNode,
        lang_config: LanguageSpec,
    ) -> list[str] | None:
        path_parts: list[str] = []
        current = func_node.parent

        while current and current.type not in lang_config.module_node_types:
            if (
                current.type not in lang_config.function_node_types
                and current.type in lang_config.class_node_types
                and not self.is_inside_method_with_object_literals(func_node)
            ):
                return None
            if name := self._ancestor_path_name(current, lang_config):
                path_parts.append(name)
            current = current.parent

        path_parts.reverse()
        return path_parts

    def _ancestor_path_name(
        self, node: ASTNode, lang_config: LanguageSpec
    ) -> str | None:
        if node.type in lang_config.function_node_types:
            # The declared name first, then the assignment-derived one.
            return self._extract_node_name(node) or self.extract_function_name(node)
        if (
            node.type in lang_config.class_node_types
            or node.type == cs.TS_METHOD_DEFINITION
        ):
            return self._extract_node_name(node)
        return None
