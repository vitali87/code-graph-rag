from __future__ import annotations

from abc import abstractmethod

from loguru import logger

from ... import constants as cs
from ... import logs as lg
from ...types_defs import ASTNode, FunctionRegistryTrieProtocol, NodeType
from ..import_processor import ImportProcessor
from ..utils import safe_decode_text


class PythonVariableAnalyzerMixin:
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol

    @abstractmethod
    def _infer_type_from_expression(
        self, node: ASTNode, module_qn: str
    ) -> str | None: ...

    def _infer_parameter_types(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        params_node = caller_node.child_by_field_name(cs.TS_FIELD_PARAMETERS)
        if not params_node:
            return

        for param in params_node.children:
            self._process_parameter(param, local_var_types, module_qn)

    def _process_parameter(
        self, param: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        match param.type:
            case cs.TS_PY_IDENTIFIER:
                self._process_untyped_parameter(param, local_var_types, module_qn)
            case cs.TS_PY_TYPED_PARAMETER:
                self._process_typed_parameter(param, local_var_types)
            case cs.TS_PY_TYPED_DEFAULT_PARAMETER:
                self._process_typed_default_parameter(param, local_var_types)

    def _process_untyped_parameter(
        self, param: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if (
            param.text is None
            or (param_name := safe_decode_text(param)) is None
            or not (
                inferred_type := self._infer_type_from_parameter_name(
                    param_name, module_qn
                )
            )
        ):
            return
        local_var_types[param_name] = inferred_type
        logger.debug(
            lg.PY_PARAM_TYPE_INFERRED.format(param=param_name, type=inferred_type)
        )

    def _process_typed_parameter(
        self, param: ASTNode, local_var_types: dict[str, str]
    ) -> None:
        param_name_node = next(
            (c for c in param.children if c.type == cs.TS_PY_IDENTIFIER), None
        )
        param_type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
        if not (
            param_name_node
            and param_type_node
            and param_name_node.text
            and param_type_node.text
            and (param_name := safe_decode_text(param_name_node))
            and (param_type := safe_decode_text(param_type_node))
        ):
            return
        local_var_types[param_name] = param_type

    def _process_typed_default_parameter(
        self, param: ASTNode, local_var_types: dict[str, str]
    ) -> None:
        param_name_node = param.child_by_field_name(cs.TS_FIELD_NAME)
        param_type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
        if not (
            param_name_node
            and param_type_node
            and param_name_node.text
            and param_type_node.text
            and (param_name := safe_decode_text(param_name_node))
            and (param_type := safe_decode_text(param_type_node))
        ):
            return
        local_var_types[param_name] = param_type

    def _infer_type_from_parameter_name(
        self, param_name: str, module_qn: str
    ) -> str | None:
        logger.debug(
            lg.PY_TYPE_INFER_ATTEMPT.format(param=param_name, module=module_qn)
        )
        available_class_names = self._collect_available_classes(module_qn)
        logger.debug(lg.PY_AVAILABLE_CLASSES.format(classes=available_class_names))
        return self._find_best_class_match(param_name, available_class_names)

    def _collect_available_classes(self, module_qn: str) -> list[str]:
        available_class_names: list[str] = []
        for qn, node_type in self.function_registry.find_with_prefix(module_qn):
            if node_type != NodeType.CLASS:
                continue
            if cs.SEPARATOR_DOT.join(qn.split(cs.SEPARATOR_DOT)[:-1]) == module_qn:
                available_class_names.append(qn.split(cs.SEPARATOR_DOT)[-1])

        if module_qn not in self.import_processor.import_mapping:
            return available_class_names

        for local_name, imported_qn in self.import_processor.import_mapping[
            module_qn
        ].items():
            if self.function_registry.get(imported_qn) == NodeType.CLASS:
                available_class_names.append(local_name)

        return available_class_names

    def _find_best_class_match(
        self, param_name: str, available_class_names: list[str]
    ) -> str | None:
        param_lower = param_name.lower()
        best_match = None
        highest_score = 0

        for class_name in available_class_names:
            score = self._calculate_match_score(param_lower, class_name.lower())
            if score > highest_score:
                highest_score = score
                best_match = class_name

        logger.debug(
            lg.PY_BEST_MATCH.format(
                param=param_name, match=best_match, score=highest_score
            )
        )
        return best_match

    def _calculate_match_score(self, param_lower: str, class_lower: str) -> int:
        if param_lower == class_lower:
            return cs.PY_SCORE_EXACT_MATCH
        if class_lower.endswith(param_lower) or param_lower.endswith(class_lower):
            return cs.PY_SCORE_SUFFIX_MATCH
        if class_lower in param_lower:
            return int(
                cs.PY_SCORE_CONTAINS_BASE * (len(class_lower) / len(param_lower))
            )
        return 0

    def _analyze_comprehension(
        self, comp_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        for child in comp_node.children:
            if child.type == cs.TS_PY_FOR_IN_CLAUSE:
                self._analyze_for_clause(child, local_var_types, module_qn)

    def _analyze_for_loop(
        self, for_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._analyze_for_clause(for_node, local_var_types, module_qn)

    def _analyze_for_clause(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if (left_node := node.child_by_field_name(cs.TS_FIELD_LEFT)) and (
            right_node := node.child_by_field_name(cs.TS_FIELD_RIGHT)
        ):
            self._infer_loop_var_from_iterable(
                left_node, right_node, local_var_types, module_qn
            )

    def _infer_loop_var_from_iterable(
        self,
        left_node: ASTNode,
        right_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        if not (loop_var := self._extract_variable_name(left_node)):
            return

        if element_type := self._infer_iterable_element_type(
            right_node, local_var_types, module_qn
        ):
            local_var_types[loop_var] = element_type
            logger.debug(
                lg.PY_LOOP_VAR_INFERRED.format(var=loop_var, type=element_type)
            )

    def _infer_iterable_element_type(
        self, iterable_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if iterable_node.type == cs.TS_PY_LIST:
            return self._infer_list_element_type(iterable_node)

        if (
            iterable_node.type != cs.TS_PY_IDENTIFIER
            or iterable_node.text is None
            or (var_name := safe_decode_text(iterable_node)) is None
        ):
            return None
        return self._infer_variable_element_type(var_name, local_var_types, module_qn)

    def _infer_list_element_type(self, list_node: ASTNode) -> str | None:
        for child in list_node.children:
            if child.type != cs.TS_PY_CALL:
                continue
            func_node = child.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if (
                func_node
                and func_node.type == cs.TS_PY_IDENTIFIER
                and func_node.text
                and (class_name := safe_decode_text(func_node))
                and class_name[0].isupper()
            ):
                return class_name
        return None

    def _infer_instance_variable_types_from_assignments(
        self,
        assignments: list[ASTNode],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        for assignment in assignments:
            self._process_self_assignment(assignment, local_var_types, module_qn)

    def _process_self_assignment(
        self, assignment: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
        right_node = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
        if not left_node or not right_node:
            return
        left_text = left_node.text
        if not (
            left_node.type == cs.TS_PY_ATTRIBUTE
            and left_text
            and (attr_name := left_text.decode(cs.ENCODING_UTF8)).startswith(
                cs.PY_SELF_PREFIX
            )
            and (
                assigned_type := self._infer_type_from_expression(right_node, module_qn)
            )
        ):
            return
        local_var_types[attr_name] = assigned_type
        logger.debug(
            lg.PY_INSTANCE_VAR_INFERRED.format(attr=attr_name, type=assigned_type)
        )

    def _analyze_self_assignments(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        stack: list[ASTNode] = [node]

        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_ASSIGNMENT:
                self._process_self_assignment(current, local_var_types, module_qn)
            stack.extend(reversed(current.children))

    def _infer_variable_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if (
            var_name in local_var_types
            and (var_type := local_var_types[var_name])
            and var_type != cs.TYPE_INFERENCE_LIST
        ):
            return var_type
        return self._infer_method_return_element_type(var_name, module_qn)

    def _infer_method_return_element_type(
        self, var_name: str, module_qn: str
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

    def _extract_variable_name(self, node: ASTNode) -> str | None:
        if node.type != cs.TS_PY_IDENTIFIER or node.text is None:
            return None
        return safe_decode_text(node) or None
