from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..constants import SEPARATOR_DOT, SupportedLanguage
from ..language_spec import LANGUAGE_FQN_SPECS, LanguageSpec
from ..types_defs import NodeType
from ..utils.fqn_resolver import resolve_fqn_from_ast
from .cpp import utils as cpp_utils
from .lua import utils as lua_utils
from .rs import utils as rs_utils
from .utils import safe_decode_text

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries


class FunctionResolution(NamedTuple):
    qualified_name: str
    name: str
    is_exported: bool


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
        file_path = self.module_qn_to_file_path.get(module_qn)

        for func_node in captures.get("function", []):
            if not isinstance(func_node, Node):
                logger.warning(f"Expected Node but got {type(func_node)}: {func_node}")
                continue
            if self._is_method(func_node, lang_config):
                continue

            resolution = self._resolve_function_identity(
                func_node, module_qn, language, lang_config, file_path
            )
            if not resolution:
                continue

            self._register_function(
                func_node, resolution, module_qn, language, lang_config
            )

    def _resolve_function_identity(
        self,
        func_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        resolution = self._try_unified_fqn_resolution(func_node, language, file_path)
        if resolution:
            return resolution

        return self._fallback_function_resolution(
            func_node, module_qn, language, lang_config
        )

    def _try_unified_fqn_resolution(
        self,
        func_node: Node,
        language: SupportedLanguage,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        fqn_config = LANGUAGE_FQN_SPECS.get(language)
        if not fqn_config or not file_path:
            return None

        func_qn = resolve_fqn_from_ast(
            func_node, file_path, self.repo_path, self.project_name, fqn_config
        )
        if not func_qn:
            return None

        func_name = func_qn.split(SEPARATOR_DOT)[-1]
        is_exported = (
            cpp_utils.is_exported(func_node)
            if language == SupportedLanguage.CPP
            else False
        )
        return FunctionResolution(func_qn, func_name, is_exported)

    def _fallback_function_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution | None:
        if language == SupportedLanguage.CPP:
            return self._resolve_cpp_function(func_node, module_qn)
        return self._resolve_generic_function(
            func_node, module_qn, language, lang_config
        )

    def _resolve_cpp_function(
        self, func_node: Node, module_qn: str
    ) -> FunctionResolution | None:
        func_name = cpp_utils.extract_function_name(func_node)
        if not func_name:
            if func_node.type == "lambda_expression":
                func_name = (
                    f"lambda_{func_node.start_point[0]}_{func_node.start_point[1]}"
                )
            else:
                return None

        func_qn = cpp_utils.build_qualified_name(func_node, module_qn, func_name)
        is_exported = cpp_utils.is_exported(func_node)
        return FunctionResolution(func_qn, func_name, is_exported)

    def _resolve_generic_function(
        self,
        func_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution:
        func_name = self._extract_function_name(func_node)

        if (
            not func_name
            and language == SupportedLanguage.LUA
            and func_node.type == "function_definition"
        ):
            func_name = self._extract_lua_assignment_function_name(func_node)

        if not func_name:
            func_name = self._generate_anonymous_function_name(func_node, module_qn)

        func_qn = self._build_function_qn(
            func_node, module_qn, func_name, language, lang_config
        )
        return FunctionResolution(func_qn, func_name, is_exported=False)

    def _build_function_qn(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        language: SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> str:
        if language == SupportedLanguage.RUST:
            return self._build_rust_function_qualified_name(
                func_node, module_qn, func_name
            )

        nested_qn = self._build_nested_qualified_name(
            func_node, module_qn, func_name, lang_config
        )
        return nested_qn or f"{module_qn}.{func_name}"

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> None:
        func_props = self._build_function_props(func_node, resolution)
        logger.info(
            f"  Found Function: {resolution.name} (qn: {resolution.qualified_name})"
        )
        self.ingestor.ensure_node_batch("Function", func_props)

        self.function_registry[resolution.qualified_name] = NodeType.FUNCTION
        if resolution.name:
            self.simple_name_lookup[resolution.name].add(resolution.qualified_name)

        self._create_function_relationships(
            func_node, resolution, module_qn, language, lang_config
        )

    def _build_function_props(
        self, func_node: Node, resolution: FunctionResolution
    ) -> dict[str, Any]:
        return {
            "qualified_name": resolution.qualified_name,
            "name": resolution.name,
            "decorators": self._extract_decorators(func_node),
            "start_line": func_node.start_point[0] + 1,
            "end_line": func_node.end_point[0] + 1,
            "docstring": self._get_docstring(func_node),
            "is_exported": resolution.is_exported,
        }

    def _create_function_relationships(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> None:
        parent_type, parent_qn = self._determine_function_parent(
            func_node, module_qn, lang_config
        )
        self.ingestor.ensure_relationship_batch(
            (parent_type, "qualified_name", parent_qn),
            "DEFINES",
            ("Function", "qualified_name", resolution.qualified_name),
        )

        if resolution.is_exported and language == SupportedLanguage.CPP:
            self.ingestor.ensure_relationship_batch(
                ("Module", "qualified_name", module_qn),
                "EXPORTS",
                ("Function", "qualified_name", resolution.qualified_name),
            )

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
        return lua_utils.extract_assigned_name(
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
        current = func_node.parent
        if not isinstance(current, Node):
            logger.warning(
                f"Unexpected parent type for node {func_node}: {type(current)}. "
                f"Skipping."
            )
            return None

        path_parts = self._collect_ancestor_path_parts(
            func_node, current, lang_config, skip_classes
        )
        if path_parts is None:
            return None

        return self._format_nested_qn(module_qn, path_parts, func_name)

    def _collect_ancestor_path_parts(
        self,
        func_node: Node,
        current: Node | None,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> list[str] | None:
        path_parts: list[str] = []

        while current and current.type not in lang_config.module_node_types:
            result = self._process_ancestor_for_path(
                func_node, current, lang_config, skip_classes
            )
            if result is False:
                return None
            if result is not None:
                path_parts.append(result)
            current = current.parent

        path_parts.reverse()
        return path_parts

    def _process_ancestor_for_path(
        self,
        func_node: Node,
        current: Node,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> str | None | Literal[False]:
        if current.type in lang_config.function_node_types:
            return self._get_name_from_function_ancestor(current)

        if current.type in lang_config.class_node_types:
            return self._handle_class_ancestor(func_node, current, skip_classes)

        if current.type == "method_definition":
            return self._extract_node_name(current)

        return None

    def _get_name_from_function_ancestor(self, node: Node) -> str | None:
        if name := self._extract_node_name(node):
            return name
        return self._extract_function_name(node)

    def _handle_class_ancestor(
        self, func_node: Node, class_node: Node, skip_classes: bool
    ) -> str | None | Literal[False]:
        if skip_classes:
            return None
        if self._is_inside_method_with_object_literals(func_node):
            return self._extract_node_name(class_node)
        return False

    def _extract_node_name(self, node: Node) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node and name_node.text is not None:
            return safe_decode_text(name_node)
        return None

    def _format_nested_qn(
        self, module_qn: str, path_parts: list[str], func_name: str
    ) -> str:
        if path_parts:
            return f"{module_qn}.{SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _build_rust_function_qualified_name(
        self, func_node: Node, module_qn: str, func_name: str
    ) -> str:
        path_parts = rs_utils.build_module_path(func_node)
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
