from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..constants import SEPARATOR_DOT, SupportedLanguage
from ..language_spec import LANGUAGE_FQN_SPECS, LanguageSpec
from ..types_defs import NodeType
from ..utils.fqn_resolver import resolve_fqn_from_ast
from .cpp_utils import (
    build_cpp_qualified_name,
    extract_cpp_function_name,
    is_cpp_exported,
)
from .lua_utils import extract_lua_assigned_name
from .rust_utils import build_rust_module_path
from .utils import safe_decode_text

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries


class FunctionIngestMixin:
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: Any
    simple_name_lookup: Any
    module_qn_to_file_path: dict[str, Path]
    _get_docstring: Callable[[Node], str | None]
    _extract_decorators: Callable[[Node], list[str]]
    _is_inside_method_with_object_literals: Callable[[Node], bool]

    def _ingest_all_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]
        lang_config: LanguageSpec = lang_queries["config"]

        query = lang_queries["functions"]
        if not query:
            return
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)

        func_nodes = captures.get("function", [])
        file_path = self.module_qn_to_file_path.get(module_qn)

        for func_node in func_nodes:
            if not isinstance(func_node, Node):
                logger.warning(
                    f"Expected Node object but got {type(func_node)}: {func_node}"
                )
                continue
            if self._is_method(func_node, lang_config):
                continue

            func_qn = None
            func_name = None
            is_exported = False

            # (H) Try unified FQN resolution first
            fqn_config = LANGUAGE_FQN_SPECS.get(language)
            if fqn_config and file_path:
                func_qn = resolve_fqn_from_ast(
                    func_node, file_path, self.repo_path, self.project_name, fqn_config
                )
                if func_qn:
                    func_name = func_qn.split(SEPARATOR_DOT)[-1]
                    if language == SupportedLanguage.CPP:
                        is_exported = is_cpp_exported(func_node)

            # (H) Fallback to legacy logic if resolution failed
            if not func_qn:
                if language == SupportedLanguage.CPP:
                    func_name = extract_cpp_function_name(func_node)
                    if not func_name:
                        if func_node.type == "lambda_expression":
                            func_name = f"lambda_{func_node.start_point[0]}_{func_node.start_point[1]}"
                        else:
                            continue
                    func_qn = build_cpp_qualified_name(func_node, module_qn, func_name)
                    is_exported = is_cpp_exported(func_node)
                else:
                    is_exported = False
                    func_name = self._extract_function_name(func_node)

                    if (
                        not func_name
                        and language == SupportedLanguage.LUA
                        and func_node.type == "function_definition"
                    ):
                        func_name = self._extract_lua_assignment_function_name(
                            func_node
                        )

                    if not func_name:
                        func_name = self._generate_anonymous_function_name(
                            func_node, module_qn
                        )

                    if language == SupportedLanguage.RUST:
                        func_qn = self._build_rust_function_qualified_name(
                            func_node, module_qn, func_name
                        )
                    else:
                        func_qn = (
                            self._build_nested_qualified_name(
                                func_node, module_qn, func_name, lang_config
                            )
                            or f"{module_qn}.{func_name}"
                        )

            decorators = self._extract_decorators(func_node)
            func_props: dict[str, Any] = {
                "qualified_name": func_qn,
                "name": func_name,
                "decorators": decorators,
                "start_line": func_node.start_point[0] + 1,
                "end_line": func_node.end_point[0] + 1,
                "docstring": self._get_docstring(func_node),
                "is_exported": is_exported,
            }
            logger.info(f"  Found Function: {func_name} (qn: {func_qn})")
            self.ingestor.ensure_node_batch("Function", func_props)

            self.function_registry[func_qn] = NodeType.FUNCTION
            if func_name:
                self.simple_name_lookup[func_name].add(func_qn)

            parent_type, parent_qn = self._determine_function_parent(
                func_node, module_qn, lang_config
            )
            self.ingestor.ensure_relationship_batch(
                (parent_type, "qualified_name", parent_qn),
                "DEFINES",
                ("Function", "qualified_name", func_qn),
            )

            if is_exported and language == SupportedLanguage.CPP:
                self.ingestor.ensure_relationship_batch(
                    ("Module", "qualified_name", module_qn),
                    "EXPORTS",
                    ("Function", "qualified_name", func_qn),
                )

    def _ingest_top_level_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
    ) -> None:
        self._ingest_all_functions(root_node, module_qn, language, queries)

    def _extract_function_name(self, func_node: Node) -> str | None:
        name_node = func_node.child_by_field_name("name")
        if name_node and name_node.text:
            return safe_decode_text(name_node)

        if func_node.type == "arrow_function":
            current = func_node.parent
            while current:
                if current.type == "variable_declarator":
                    for child in current.children:
                        if child.type == "identifier" and child.text:
                            return safe_decode_text(child)
                current = current.parent

        return None

    def _generate_anonymous_function_name(self, func_node: Node, module_qn: str) -> str:
        parent = func_node.parent
        if parent and parent.type == "parenthesized_expression":
            grandparent = parent.parent
            if (
                grandparent
                and grandparent.type == "call_expression"
                and grandparent.child_by_field_name("function") == parent
            ):
                func_type = "arrow" if func_node.type == "arrow_function" else "func"
                return f"iife_{func_type}_{func_node.start_point[0]}_{func_node.start_point[1]}"

        if (
            parent
            and parent.type == "call_expression"
            and parent.child_by_field_name("function") == func_node
        ):
            return f"iife_direct_{func_node.start_point[0]}_{func_node.start_point[1]}"

        return f"anonymous_{func_node.start_point[0]}_{func_node.start_point[1]}"

    def _extract_lua_assignment_function_name(self, func_node: Node) -> str | None:
        return extract_lua_assigned_name(
            func_node, accepted_var_types=("dot_index_expression", "identifier")
        )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
        skip_classes: bool = False,
    ) -> str | None:
        path_parts: list[str] = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                f"Unexpected parent type for node {func_node}: {type(current)}. "
                f"Skipping."
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    if name_node.text is not None:
                        if decoded := safe_decode_text(name_node):
                            path_parts.append(decoded)
                else:
                    func_name_from_assignment = self._extract_function_name(current)
                    if func_name_from_assignment:
                        path_parts.append(func_name_from_assignment)
            elif current.type in lang_config.class_node_types:
                if skip_classes:
                    pass
                elif self._is_inside_method_with_object_literals(func_node):
                    if name_node := current.child_by_field_name("name"):
                        if name_node.text is not None:
                            if decoded := safe_decode_text(name_node):
                                path_parts.append(decoded)
                else:
                    return None
            elif current.type == "method_definition":
                if name_node := current.child_by_field_name("name"):
                    if name_node.text is not None:
                        if decoded := safe_decode_text(name_node):
                            path_parts.append(decoded)

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}.{SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _build_rust_method_qualified_name(
        self, method_node: Node, module_qn: str, method_name: str
    ) -> str:
        path_parts = build_rust_module_path(method_node, include_impl_targets=True)
        if path_parts:
            return f"{module_qn}.{SEPARATOR_DOT.join(path_parts)}.{method_name}"
        return f"{module_qn}.{method_name}"

    def _build_rust_function_qualified_name(
        self, func_node: Node, module_qn: str, func_name: str
    ) -> str:
        path_parts = build_rust_module_path(func_node)
        if path_parts:
            return f"{module_qn}.{SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        current = func_node.parent
        if not isinstance(current, Node):
            return False

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.class_node_types:
                return True
            current = current.parent
        return False

    def _determine_function_parent(
        self, func_node: Node, module_qn: str, lang_config: LanguageSpec
    ) -> tuple[str, str]:
        current = func_node.parent
        if not isinstance(current, Node):
            return "Module", module_qn

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    parent_text = name_node.text
                    if parent_text is None:
                        continue
                    if parent_func_name := safe_decode_text(name_node):
                        if parent_func_qn := self._build_nested_qualified_name(
                            current, module_qn, parent_func_name, lang_config
                        ):
                            return "Function", parent_func_qn
                break

            current = current.parent

        return "Module", module_qn
