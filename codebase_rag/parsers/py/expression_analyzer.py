from __future__ import annotations

import re
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as lg
from ...types_defs import FunctionRegistryTrieProtocol, NodeType, SimpleNameLookup
from ..import_processor import ImportProcessor
from ..utils import safe_decode_text
from .utils import resolve_class_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ..factory import ASTCacheProtocol


class PythonExpressionAnalyzerMixin:
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol

    _method_return_type_cache: dict[str, str | None]
    _type_inference_in_progress: set[str]

    _analyze_self_assignments: Callable[[Node, dict[str, str], str], None]
    _find_method_ast_node: Callable[[str], Node | None]
    _analyze_method_return_statements: Callable[[Node, str], str | None]
    build_local_variable_type_map: Callable[[Node, str], dict[str, str]]

    def _infer_type_from_expression(self, node: Node, module_qn: str) -> str | None:
        if node.type == cs.TS_PY_CALL:
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == cs.TS_PY_IDENTIFIER:
                func_text = func_node.text
                if func_text is not None:
                    class_name = safe_decode_text(func_node)
                    if class_name and len(class_name) > 0 and class_name[0].isupper():
                        return class_name

            elif func_node and func_node.type == cs.TS_PY_ATTRIBUTE:
                method_call_text = self._extract_full_method_call(func_node)
                if method_call_text:
                    return self._infer_method_call_return_type(
                        method_call_text, module_qn, local_var_types=None
                    )

        elif node.type == cs.TS_PY_LIST_COMPREHENSION:
            if body_node := node.child_by_field_name("body"):
                return self._infer_type_from_expression(body_node, module_qn)

        return None

    def _infer_type_from_expression_simple(
        self, node: Node, module_qn: str
    ) -> str | None:
        if node.type == cs.TS_PY_CALL:
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == cs.TS_PY_IDENTIFIER:
                func_text = func_node.text
                if func_text is not None:
                    class_name = safe_decode_text(func_node)
                    if class_name and len(class_name) > 0 and class_name[0].isupper():
                        return class_name

        elif node.type == cs.TS_PY_LIST_COMPREHENSION:
            body_node = node.child_by_field_name("body")
            if body_node:
                return self._infer_type_from_expression_simple(body_node, module_qn)

        return None

    def _infer_type_from_expression_complex(
        self, node: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> str | None:
        if node.type == cs.TS_PY_CALL:
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == cs.TS_PY_ATTRIBUTE:
                method_call_text = self._extract_full_method_call(func_node)
                if method_call_text:
                    return self._infer_method_call_return_type(
                        method_call_text, module_qn, local_var_types
                    )

        return None

    def _extract_full_method_call(self, attr_node: Node) -> str | None:
        if attr_node.text:
            decoded = safe_decode_text(attr_node)
            if decoded:
                return decoded
        return None

    def _infer_method_call_return_type(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        cache_key = f"{module_qn}:{method_call}"

        # (H) Recursion guard: prevent infinite loops in recursive type inference.
        if cache_key in self._type_inference_in_progress:
            logger.debug(lg.PY_RECURSION_GUARD.format(method=method_call))
            return None

        self._type_inference_in_progress.add(cache_key)
        try:
            if cs.SEPARATOR_DOT in method_call and self._is_method_chain(method_call):
                return self._infer_chained_call_return_type_fixed(
                    method_call, module_qn, local_var_types
                )

            return self._infer_method_return_type(
                method_call, module_qn, local_var_types
            )
        finally:
            self._type_inference_in_progress.discard(cache_key)

    def _is_method_chain(self, call_name: str) -> bool:
        if "(" in call_name and ")" in call_name:
            return bool(re.search(r"\)\.[^)]*$", call_name))
        return False

    def _infer_chained_call_return_type_fixed(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        match = re.search(r"\.([^.()]+)$", call_name)
        if not match:
            return None

        final_method = match.group(1)

        object_expr = call_name[: match.start()]

        object_type = self._infer_object_type_for_chained_call(
            object_expr, module_qn, local_var_types
        )

        if object_type:
            full_object_type = object_type
            if cs.SEPARATOR_DOT not in object_type:
                resolved_class = self._resolve_class_name(object_type, module_qn)
                if resolved_class:
                    full_object_type = resolved_class

            method_qn = f"{full_object_type}.{final_method}"
            return self._get_method_return_type_from_ast(method_qn)

        return None

    def _infer_object_type_for_chained_call(
        self,
        object_expr: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if (
            "(" not in object_expr
            and local_var_types
            and object_expr in local_var_types
        ):
            var_type = local_var_types[object_expr]
            return var_type

        if "(" in object_expr and ")" in object_expr:
            return self._infer_method_call_return_type(
                object_expr, module_qn, local_var_types
            )

        return None

    def _infer_chained_call_return_type(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        return self._infer_chained_call_return_type_fixed(
            call_name, module_qn, local_var_types
        )

    def _infer_expression_return_type(
        self,
        expression: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if "(" not in expression and local_var_types and expression in local_var_types:
            var_type = local_var_types[expression]
            if module_qn in self.import_processor.import_mapping:
                import_map = self.import_processor.import_mapping[module_qn]
                if var_type in import_map:
                    return import_map[var_type]
            return self._resolve_class_name(var_type, module_qn)

        return self._infer_method_call_return_type(
            expression, module_qn, local_var_types
        )

    def _get_method_return_type_from_ast(self, method_qn: str) -> str | None:
        if method_qn in self._method_return_type_cache:
            return self._method_return_type_cache[method_qn]
        if method_qn in self._type_inference_in_progress:
            logger.debug(lg.PY_RECURSION_GUARD_QN.format(method_qn=method_qn))
            return None

        self._type_inference_in_progress.add(method_qn)
        try:
            method_node = self._find_method_ast_node(method_qn)
            if not method_node:
                result = None
            else:
                result = self._analyze_method_return_statements(method_node, method_qn)

            self._method_return_type_cache[method_qn] = result
            return result
        finally:
            self._type_inference_in_progress.discard(method_qn)

    def _extract_object_type_from_call(
        self,
        object_part: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if local_var_types and object_part in local_var_types:
            return local_var_types[object_part]

        return None

    def _infer_method_return_type(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        try:
            method_qn = self._resolve_method_qualified_name(
                method_call, module_qn, local_var_types
            )
            if not method_qn:
                return None

            method_node = self._find_method_ast_node(method_qn)
            if not method_node:
                return None

            return self._analyze_method_return_statements(method_node, method_qn)

        except Exception as e:
            logger.debug(lg.PY_INFER_RETURN_FAILED.format(method=method_call, error=e))
            return None

    def _resolve_method_qualified_name(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if cs.SEPARATOR_DOT not in method_call:
            return None

        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            return None

        if len(parts) == 2:
            class_name, method_name_with_args = parts

            method_name = (
                method_name_with_args.split("(")[0]
                if "(" in method_name_with_args
                else method_name_with_args
            )

            if local_var_types and class_name in local_var_types:
                var_type = local_var_types[class_name]
                return self._resolve_class_method(var_type, method_name, module_qn)

            return self._resolve_class_method(class_name, method_name, module_qn)

        if parts[0] == cs.PY_KEYWORD_SELF and len(parts) >= 3:
            attribute_name = parts[1]
            method_name = parts[-1]

            attribute_type = self._infer_attribute_type(attribute_name, module_qn)
            if attribute_type:
                return self._resolve_class_method(
                    attribute_type, method_name, module_qn
                )

        if len(parts) >= 3:
            potential_class = parts[-2]
            method_name = parts[-1]
            return self._resolve_class_method(potential_class, method_name, module_qn)

        return None

    def _resolve_class_method(
        self, class_name: str, method_name: str, module_qn: str
    ) -> str | None:
        local_class_qn = f"{module_qn}.{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == NodeType.CLASS
        ):
            method_qn = f"{local_class_qn}.{method_name}"
            if (
                method_qn in self.function_registry
                and self.function_registry[method_qn] == NodeType.METHOD
            ):
                return method_qn

        if module_qn in self.import_processor.import_mapping:
            import_mapping = self.import_processor.import_mapping[module_qn]

            if class_name in import_mapping:
                imported_class_qn = import_mapping[class_name]
                if (
                    imported_class_qn in self.function_registry
                    and self.function_registry[imported_class_qn] == NodeType.CLASS
                ):
                    method_qn = f"{imported_class_qn}.{method_name}"
                    if (
                        method_qn in self.function_registry
                        and self.function_registry[method_qn] == NodeType.METHOD
                    ):
                        return method_qn

        if class_name in self.simple_name_lookup:
            for qn in self.simple_name_lookup[class_name]:
                if self.function_registry.get(qn) == NodeType.CLASS:
                    method_qn = f"{qn}.{method_name}"
                    if (
                        method_qn in self.function_registry
                        and self.function_registry[method_qn] == NodeType.METHOD
                    ):
                        logger.debug(
                            lg.PY_RESOLVED_METHOD.format(
                                class_name=class_name,
                                method_name=method_name,
                                method_qn=method_qn,
                            )
                        )
                        return method_qn

        return None

    def _infer_attribute_type(self, attribute_name: str, module_qn: str) -> str | None:
        try:
            if module_qn in self.module_qn_to_file_path:
                file_path = self.module_qn_to_file_path[module_qn]
                if file_path in self.ast_cache:
                    root_node, language = self.ast_cache[file_path]
                    if language == cs.SupportedLanguage.PYTHON:
                        instance_vars: dict[str, str] = {}
                        self._analyze_self_assignments(
                            root_node, instance_vars, module_qn
                        )

                        full_attr_name = f"{cs.PY_SELF_PREFIX}{attribute_name}"
                        if full_attr_name in instance_vars:
                            return instance_vars[full_attr_name]

        except Exception as e:
            logger.debug(lg.PY_INFER_ATTR_FAILED.format(attr=attribute_name, error=e))

        if "_" in attribute_name:
            parts = attribute_name.split("_")
            class_name = "".join(word.capitalize() for word in parts)
        else:
            class_name = attribute_name.capitalize()

        return self._find_class_in_scope(class_name, module_qn)

    def _find_class_in_scope(self, class_name: str, module_qn: str) -> str | None:
        local_class_qn = f"{module_qn}.{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == NodeType.CLASS
        ):
            return class_name

        if module_qn in self.import_processor.import_mapping:
            import_mapping = self.import_processor.import_mapping[module_qn]
            for local_name, imported_qn in import_mapping.items():
                if (
                    local_name == class_name
                    and imported_qn in self.function_registry
                    and self.function_registry[imported_qn] == NodeType.CLASS
                ):
                    return class_name

        if class_name in self.simple_name_lookup:
            for qn in self.simple_name_lookup[class_name]:
                if self.function_registry.get(qn) == NodeType.CLASS:
                    return class_name

        return None

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )
