from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as lg
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ..import_processor import ImportProcessor
from ..utils import safe_decode_text

if TYPE_CHECKING:
    from collections.abc import Callable


class PythonVariableAnalyzerMixin:
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol

    _infer_type_from_expression: Callable[[Node, str], str | None]

    def _infer_parameter_types(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        params_node = caller_node.child_by_field_name("parameters")
        if not params_node:
            return

        for param in params_node.children:
            if param.type == cs.TS_PY_IDENTIFIER:
                param_text = param.text
                if param_text is not None:
                    param_name = safe_decode_text(param)

                    if param_name is not None:
                        inferred_type = self._infer_type_from_parameter_name(
                            param_name, module_qn
                        )
                        if inferred_type:
                            local_var_types[param_name] = inferred_type
                            logger.debug(
                                lg.PY_PARAM_TYPE_INFERRED.format(
                                    param=param_name, type=inferred_type
                                )
                            )

            elif param.type == cs.TS_PY_TYPED_PARAMETER:
                param_name_node = param.child_by_field_name("name")
                param_type_node = param.child_by_field_name("type")
                if (
                    param_name_node
                    and param_type_node
                    and param_name_node.text
                    and param_type_node.text
                ):
                    param_name = safe_decode_text(param_name_node)
                    param_type = safe_decode_text(param_type_node)
                    if param_name is not None and param_type is not None:
                        local_var_types[param_name] = param_type

    def _infer_type_from_parameter_name(
        self, param_name: str, module_qn: str
    ) -> str | None:
        logger.debug(
            lg.PY_TYPE_INFER_ATTEMPT.format(param=param_name, module=module_qn)
        )
        available_class_names = []

        for qn, node_type in self.function_registry.find_with_prefix(module_qn):
            if node_type == NodeType.CLASS:
                if cs.SEPARATOR_DOT.join(qn.split(cs.SEPARATOR_DOT)[:-1]) == module_qn:
                    available_class_names.append(qn.split(cs.SEPARATOR_DOT)[-1])

        if module_qn in self.import_processor.import_mapping:
            for local_name, imported_qn in self.import_processor.import_mapping[
                module_qn
            ].items():
                if self.function_registry.get(imported_qn) == NodeType.CLASS:
                    available_class_names.append(local_name)

        logger.debug(lg.PY_AVAILABLE_CLASSES.format(classes=available_class_names))

        param_lower = param_name.lower()
        best_match = None
        highest_score = 0

        for class_name in available_class_names:
            class_lower = class_name.lower()
            score = 0

            if param_lower == class_lower:
                score = 100
            elif class_lower.endswith(param_lower) or param_lower.endswith(class_lower):
                score = 90
            elif class_lower in param_lower:
                score = int(80 * (len(class_lower) / len(param_lower)))

            if score > highest_score:
                highest_score = score
                best_match = class_name

        logger.debug(
            lg.PY_BEST_MATCH.format(
                param=param_name, match=best_match, score=highest_score
            )
        )
        return best_match

    def _analyze_comprehension(
        self, comp_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        for child in comp_node.children:
            if child.type == cs.TS_PY_FOR_IN_CLAUSE:
                self._analyze_for_in_clause(child, local_var_types, module_qn)

    def _analyze_for_loop(
        self, for_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = for_node.child_by_field_name("left")
        right_node = for_node.child_by_field_name("right")

        if left_node and right_node:
            self._infer_loop_var_from_iterable(
                left_node, right_node, local_var_types, module_qn
            )

    def _analyze_for_in_clause(
        self, for_in_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = for_in_node.child_by_field_name("left")
        right_node = for_in_node.child_by_field_name("right")

        if left_node and right_node:
            self._infer_loop_var_from_iterable(
                left_node, right_node, local_var_types, module_qn
            )

    def _infer_loop_var_from_iterable(
        self,
        left_node: Node,
        right_node: Node,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        loop_var = self._extract_variable_name(left_node)
        if not loop_var:
            return

        element_type = self._infer_iterable_element_type(
            right_node, local_var_types, module_qn
        )
        if element_type:
            local_var_types[loop_var] = element_type
            logger.debug(
                lg.PY_LOOP_VAR_INFERRED.format(var=loop_var, type=element_type)
            )

    def _infer_iterable_element_type(
        self, iterable_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if iterable_node.type == cs.TS_PY_LIST:
            return self._infer_list_element_type(
                iterable_node, local_var_types, module_qn
            )

        if iterable_node.type == cs.TS_PY_IDENTIFIER:
            var_text = iterable_node.text
            if var_text is not None:
                var_name = safe_decode_text(iterable_node)
                if var_name is not None:
                    return self._infer_variable_element_type(
                        var_name, local_var_types, module_qn
                    )

        return None

    def _infer_list_element_type(
        self, list_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        for child in list_node.children:
            if child.type == cs.TS_PY_CALL:
                func_node = child.child_by_field_name("function")
                if func_node and func_node.type == cs.TS_PY_IDENTIFIER:
                    func_text = func_node.text
                    if func_text is not None:
                        class_name = safe_decode_text(func_node)
                        if class_name and class_name[0].isupper():
                            return class_name
        return None

    def _infer_instance_variable_types_from_assignments(
        self, assignments: list[Node], local_var_types: dict[str, str], module_qn: str
    ) -> None:
        for assignment in assignments:
            left_node = assignment.child_by_field_name("left")
            right_node = assignment.child_by_field_name("right")

            if left_node and right_node and left_node.type == cs.TS_PY_ATTRIBUTE:
                left_text = left_node.text
                if left_text and left_text.decode(cs.ENCODING_UTF8).startswith(
                    cs.PY_SELF_PREFIX
                ):
                    attr_name = left_text.decode(cs.ENCODING_UTF8)
                    assigned_type = self._infer_type_from_expression(
                        right_node, module_qn
                    )
                    if assigned_type:
                        local_var_types[attr_name] = assigned_type
                        logger.debug(
                            lg.PY_INSTANCE_VAR_INFERRED.format(
                                attr=attr_name, type=assigned_type
                            )
                        )

    def _analyze_self_assignments(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()

            if current.type == cs.TS_PY_ASSIGNMENT:
                left_node = current.child_by_field_name("left")
                right_node = current.child_by_field_name("right")

                if left_node and right_node and left_node.type == cs.TS_PY_ATTRIBUTE:
                    left_text = left_node.text
                    left_decoded = safe_decode_text(left_node)
                    if (
                        left_text
                        and left_decoded
                        and left_decoded.startswith(cs.PY_SELF_PREFIX)
                    ):
                        attr_name = left_decoded
                        assigned_type = self._infer_type_from_expression(
                            right_node, module_qn
                        )
                        if assigned_type:
                            local_var_types[attr_name] = assigned_type
                            logger.debug(
                                lg.PY_INSTANCE_VAR_INFERRED.format(
                                    attr=attr_name, type=assigned_type
                                )
                            )

            stack.extend(reversed(current.children))

    def _infer_variable_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if var_name in local_var_types:
            var_type = local_var_types[var_name]
            if var_type and var_type != cs.TYPE_INFERENCE_LIST:
                return var_type

        return self._infer_method_return_element_type(
            var_name, local_var_types, module_qn
        )

    def _infer_method_return_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if cs.PY_VAR_PATTERN_ALL in var_name or var_name.endswith(
            cs.PY_VAR_SUFFIX_PLURAL
        ):
            return self._analyze_repository_item_type(module_qn)

        return None

    def _analyze_repository_item_type(self, module_qn: str) -> str | None:
        repo_qn_patterns = [
            f"{module_qn.split(cs.SEPARATOR_DOT)[0]}{cs.PY_MODELS_BASE_PATH}{cs.PY_CLASS_REPOSITORY}",
            cs.PY_CLASS_REPOSITORY,
        ]

        for repo_qn in repo_qn_patterns:
            create_method = f"{repo_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_CREATE}"
            if create_method in self.function_registry:
                return cs.TYPE_INFERENCE_BASE_MODEL

        return None

    def _extract_variable_name(self, node: Node) -> str | None:
        if node.type == cs.TS_PY_IDENTIFIER:
            text = node.text
            if text is not None:
                decoded = safe_decode_text(node)
                if decoded:
                    return decoded
        return None
