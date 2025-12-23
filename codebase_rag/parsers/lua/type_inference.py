from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionRegistryTrieProtocol, TreeSitterNodeProtocol
from ..utils import safe_decode_text

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor


class LuaTypeInferenceEngine:
    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        project_name: str,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name

    def build_local_variable_type_map(
        self, caller_node: TreeSitterNodeProtocol, module_qn: str
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        stack: list[TreeSitterNodeProtocol] = [caller_node]

        while stack:
            current = stack.pop()

            if current.type == cs.TS_LUA_VARIABLE_DECLARATION:
                if assignment := next(
                    (
                        child
                        for child in current.children
                        if child.type == cs.TS_LUA_ASSIGNMENT_STATEMENT
                    ),
                    None,
                ):
                    var_names = []
                    var_values = []

                    for child in assignment.children:
                        if child.type == cs.TS_LUA_VARIABLE_LIST:
                            for var_node in child.children:
                                if var_node.type == cs.TS_LUA_IDENTIFIER:
                                    if decoded_name := safe_decode_text(var_node):
                                        var_names.append(decoded_name)
                        elif child.type == cs.TS_LUA_EXPRESSION_LIST:
                            var_values.extend(
                                expr_node
                                for expr_node in child.children
                                if expr_node.type == cs.TS_LUA_FUNCTION_CALL
                            )
                    for i, var_name in enumerate(var_names):
                        if i < len(var_values):
                            value_node = var_values[i]
                            if var_type := self._infer_lua_variable_type_from_value(
                                value_node, module_qn
                            ):
                                local_var_types[var_name] = var_type
                                logger.debug(
                                    ls.LUA_VAR_INFERRED.format(
                                        var_name=var_name, var_type=var_type
                                    )
                                )

            stack.extend(reversed(current.children))

        logger.debug(ls.LUA_VAR_TYPE_MAP_BUILT.format(count=len(local_var_types)))
        return local_var_types

    def _infer_lua_variable_type_from_value(
        self, value_node: TreeSitterNodeProtocol, module_qn: str
    ) -> str | None:
        if value_node.type == cs.TS_LUA_FUNCTION_CALL:
            for child in value_node.children:
                if child.type == cs.TS_LUA_METHOD_INDEX_EXPRESSION:
                    class_name = None
                    method_name = None

                    for grandchild in child.children:
                        if grandchild.type == cs.TS_LUA_IDENTIFIER:
                            if class_name is None:
                                class_name = safe_decode_text(grandchild)
                            else:
                                method_name = safe_decode_text(grandchild)

                    if class_name and method_name:
                        if class_qn := self._resolve_lua_class_name(
                            class_name, module_qn
                        ):
                            logger.debug(
                                ls.LUA_TYPE_INFERENCE_RETURN.format(
                                    class_name=class_name,
                                    method_name=method_name,
                                    class_qn=class_qn,
                                )
                            )
                            return class_qn

        return None

    def _resolve_lua_class_name(self, class_name: str, module_qn: str) -> str | None:
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]
                full_class_qn = f"{imported_qn}{cs.SEPARATOR_DOT}{class_name}"
                return full_class_qn

        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if local_class_qn in self.function_registry:
            return local_class_qn

        method_prefix = f"{local_class_qn}{cs.LUA_METHOD_SEPARATOR}"
        return next(
            (
                local_class_qn
                for qn, _ in self.function_registry.find_with_prefix(local_class_qn)
                if qn.startswith(method_prefix)
            ),
            None,
        )
