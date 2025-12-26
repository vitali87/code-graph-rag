from collections.abc import Callable

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import ASTNode, FunctionRegistryTrieProtocol, NodeType
from ..import_processor import ImportProcessor
from ..utils import safe_decode_text
from . import utils as ut


class JsTypeInferenceEngine:
    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        project_name: str,
        find_method_ast_node_func: Callable[[str], ASTNode | None],
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name
        self._find_method_ast_node = find_method_ast_node_func

    def build_local_variable_type_map(
        self, caller_node: ASTNode, module_qn: str
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        stack: list[ASTNode] = [caller_node]

        declarator_count = 0

        while stack:
            current = stack.pop()

            if current.type == cs.TS_VARIABLE_DECLARATOR:
                declarator_count += 1
                name_node = current.child_by_field_name("name")
                value_node = current.child_by_field_name("value")

                if name_node and value_node:
                    var_name_text = name_node.text
                    if var_name_text:
                        var_name = safe_decode_text(name_node)
                        if var_name is not None:
                            logger.debug(
                                ls.JS_VAR_DECLARATOR_FOUND.format(
                                    var_name=var_name, module_qn=module_qn
                                )
                            )

                            if var_type := self._infer_js_variable_type_from_value(
                                value_node, module_qn
                            ):
                                local_var_types[var_name] = var_type
                                logger.debug(
                                    ls.JS_VAR_INFERRED.format(
                                        var_name=var_name, var_type=var_type
                                    )
                                )
                            else:
                                logger.debug(
                                    ls.JS_VAR_INFER_FAILED.format(var_name=var_name)
                                )

            stack.extend(reversed(current.children))

        logger.debug(
            ls.JS_VAR_TYPE_MAP_BUILT.format(
                count=len(local_var_types), declarator_count=declarator_count
            )
        )
        return local_var_types

    def _infer_js_variable_type_from_value(
        self, value_node: ASTNode, module_qn: str
    ) -> str | None:
        logger.debug(ls.JS_INFER_VALUE_NODE.format(node_type=value_node.type))

        if value_node.type == cs.TS_NEW_EXPRESSION:
            if class_name := ut.extract_constructor_name(value_node):
                class_qn = self._resolve_js_class_name(class_name, module_qn)
                return class_qn or class_name

        elif value_node.type == cs.TS_CALL_EXPRESSION:
            func_node = value_node.child_by_field_name("function")
            func_type = func_node.type if func_node else cs.STR_NONE
            logger.debug(ls.JS_CALL_EXPR_FUNC_NODE.format(func_type=func_type))

            if func_node and func_node.type == cs.TS_MEMBER_EXPRESSION:
                method_call_text = ut.extract_method_call(func_node)
                logger.debug(
                    ls.JS_EXTRACTED_METHOD_CALL.format(method_call=method_call_text)
                )
                if method_call_text:
                    if inferred_type := self._infer_js_method_return_type(
                        method_call_text, module_qn
                    ):
                        logger.debug(
                            ls.JS_TYPE_INFERRED.format(
                                method_call=method_call_text,
                                inferred_type=inferred_type,
                            )
                        )
                        return inferred_type
                    logger.debug(
                        ls.JS_RETURN_TYPE_INFER_FAILED.format(
                            method_call=method_call_text
                        )
                    )

            elif func_node and func_node.type == cs.TS_IDENTIFIER:
                func_name = func_node.text
                if func_name:
                    return safe_decode_text(func_node)

        logger.debug(ls.JS_NO_PATTERN_MATCHED.format(node_type=value_node.type))
        return None

    def _infer_js_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            logger.debug(ls.JS_METHOD_CALL_INVALID.format(method_call=method_call))
            return None

        class_name, method_name = parts

        class_qn = self._resolve_js_class_name(class_name, module_qn)
        if not class_qn:
            logger.debug(
                ls.JS_CLASS_RESOLVE_FAILED.format(
                    class_name=class_name, module_qn=module_qn
                )
            )
            return None

        logger.debug(
            ls.JS_CLASS_RESOLVED.format(class_name=class_name, class_qn=class_qn)
        )

        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        logger.debug(ls.JS_LOOKING_FOR_METHOD.format(method_qn=method_qn))

        method_node = self._find_method_ast_node(method_qn)
        if not method_node:
            logger.debug(ls.JS_METHOD_AST_NOT_FOUND.format(method_qn=method_qn))
            return None

        return_type = self._analyze_return_statements(method_node, method_qn)
        logger.debug(
            ls.JS_RETURN_ANALYZED.format(method_qn=method_qn, return_type=return_type)
        )
        return return_type

    def _resolve_js_class_name(self, class_name: str, module_qn: str) -> str | None:
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]

                full_class_qn = f"{imported_qn}{cs.SEPARATOR_DOT}{class_name}"
                if (
                    full_class_qn in self.function_registry
                    and self.function_registry[full_class_qn] == NodeType.CLASS
                ):
                    return full_class_qn

                return imported_qn

        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == NodeType.CLASS
        ):
            return local_class_qn

        return None

    def _analyze_return_statements(
        self, method_node: ASTNode, method_qn: str
    ) -> str | None:
        return_nodes: list[ASTNode] = []
        ut.find_return_statements(method_node, return_nodes)

        for return_node in return_nodes:
            for child in return_node.children:
                if child.type == cs.TS_RETURN:
                    continue

                if inferred_type := ut.analyze_return_expression(child, method_qn):
                    return inferred_type

        return None
