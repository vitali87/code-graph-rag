import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as lg
from ..types_defs import (
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    NodeType,
    SimpleNameLookup,
)
from .import_processor import ImportProcessor
from .java_type_inference import JavaTypeInferenceEngine
from .js_type_inference import JsTypeInferenceEngine
from .lua_type_inference import LuaTypeInferenceEngine
from .python_utils import resolve_class_name
from .utils import safe_decode_text

if TYPE_CHECKING:
    from .factory import ASTCacheProtocol

_JS_TYPESCRIPT_LANGUAGES = {cs.SupportedLanguage.JS, cs.SupportedLanguage.TS}


class TypeInferenceEngine:
    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        repo_path: Path,
        project_name: str,
        ast_cache: "ASTCacheProtocol",
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: SimpleNameLookup,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance = class_inheritance
        self.simple_name_lookup = simple_name_lookup

        self._java_type_inference: JavaTypeInferenceEngine | None = None
        self._lua_type_inference: LuaTypeInferenceEngine | None = None
        self._js_type_inference: JsTypeInferenceEngine | None = None

        self._method_return_type_cache: dict[str, str | None] = {}
        self._type_inference_in_progress: set[str] = set()

    @property
    def java_type_inference(self) -> JavaTypeInferenceEngine:
        if self._java_type_inference is None:
            self._java_type_inference = JavaTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                repo_path=self.repo_path,
                project_name=self.project_name,
                ast_cache=self.ast_cache,
                queries=self.queries,
                module_qn_to_file_path=self.module_qn_to_file_path,
                class_inheritance=self.class_inheritance,
                simple_name_lookup=self.simple_name_lookup,
            )
        return self._java_type_inference

    @property
    def lua_type_inference(self) -> LuaTypeInferenceEngine:
        if self._lua_type_inference is None:
            self._lua_type_inference = LuaTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                project_name=self.project_name,
            )
        return self._lua_type_inference

    @property
    def js_type_inference(self) -> JsTypeInferenceEngine:
        if self._js_type_inference is None:
            self._js_type_inference = JsTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                project_name=self.project_name,
                find_method_ast_node_func=self._find_method_ast_node,
            )
        return self._js_type_inference

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str, language: cs.SupportedLanguage
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}

        match language:
            case cs.SupportedLanguage.PYTHON:
                pass
            case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS:
                return self.js_type_inference.build_js_local_variable_type_map(
                    caller_node, module_qn, language
                )
            case cs.SupportedLanguage.JAVA:
                return self.java_type_inference.build_java_variable_type_map(
                    caller_node, module_qn
                )
            case cs.SupportedLanguage.LUA:
                return self.lua_type_inference.build_lua_local_variable_type_map(
                    caller_node, module_qn
                )
            case _:
                return local_var_types

        try:
            self._infer_parameter_types(caller_node, local_var_types, module_qn)
            # (H) Single-pass traversal avoids O(5*N) multiple traversals for type inference.
            self._traverse_single_pass(caller_node, local_var_types, module_qn)

        except Exception as e:
            logger.debug(lg.PY_BUILD_VAR_MAP_FAILED.format(error=e))

        return local_var_types

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

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _infer_loop_variable_types(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._find_comprehensions(caller_node, local_var_types, module_qn)
        self._find_for_loops(caller_node, local_var_types, module_qn)

    def _find_comprehensions(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if node.type == cs.TS_PY_LIST_COMPREHENSION:
            self._analyze_comprehension(node, local_var_types, module_qn)

        for child in node.children:
            self._find_comprehensions(child, local_var_types, module_qn)

    def _find_for_loops(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if node.type == cs.TS_PY_FOR_STATEMENT:
            self._analyze_for_loop(node, local_var_types, module_qn)

        for child in node.children:
            self._find_for_loops(child, local_var_types, module_qn)

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

        elif iterable_node.type == cs.TS_PY_IDENTIFIER:
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
                        if (
                            class_name
                            and len(class_name) > 0
                            and class_name[0].isupper()
                        ):
                            return str(class_name)
        return None

    def _infer_instance_variable_types(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._analyze_self_assignments(caller_node, local_var_types, module_qn)
        self._analyze_class_init_assignments(caller_node, local_var_types, module_qn)

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

    def _analyze_class_init_assignments(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        class_node = self._find_containing_class(caller_node)
        if not class_node:
            logger.debug(lg.PY_NO_CONTAINING_CLASS)
            return

        init_method = self._find_init_method(class_node)
        if not init_method:
            logger.debug(lg.PY_NO_INIT_METHOD)
            return

        logger.debug(lg.PY_FOUND_INIT)
        self._analyze_self_assignments(init_method, local_var_types, module_qn)

    def _find_containing_class(self, method_node: Node) -> Node | None:
        current = method_node.parent
        level = 1
        while current:
            logger.debug(
                lg.PY_SEARCHING_LEVEL.format(level=level, node_type=current.type)
            )
            if current.type == cs.TS_PY_CLASS_DEFINITION:
                logger.debug(lg.PY_FOUND_CLASS_AT_LEVEL.format(level=level))
                return current
            current = current.parent
            level += 1
            if level > 10:
                break
        logger.debug(lg.PY_NO_CLASS_IN_HIERARCHY)
        return None

    def _find_init_method(self, class_node: Node) -> Node | None:
        logger.debug(lg.PY_SEARCHING_INIT.format(count=len(class_node.children)))

        class_body = None
        for child in class_node.children:
            logger.debug(lg.PY_CHILD_TYPE.format(type=child.type))
            if child.type == cs.TS_PY_BLOCK:
                class_body = child
                break

        if not class_body:
            logger.debug(lg.PY_NO_CLASS_BODY)
            return None

        logger.debug(lg.PY_SEARCHING_BODY.format(count=len(class_body.children)))
        for child in class_body.children:
            logger.debug(lg.PY_BODY_CHILD.format(type=child.type))
            if child.type == cs.TS_PY_FUNCTION_DEFINITION:
                name_node = child.child_by_field_name("name")
                if name_node and name_node.text:
                    method_name = safe_decode_text(name_node)
                    logger.debug(lg.PY_FOUND_METHOD.format(name=method_name))
                    if method_name == cs.PY_METHOD_INIT:
                        logger.debug(lg.PY_FOUND_INIT_METHOD)
                        return child
        logger.debug(lg.PY_INIT_NOT_FOUND)
        return None

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
        if "all_" in var_name or var_name.endswith("s"):
            return self._analyze_repository_item_type(module_qn)

        return None

    def _analyze_repository_item_type(self, module_qn: str) -> str | None:
        repo_qn_patterns = [
            f"{module_qn.split(cs.SEPARATOR_DOT)[0]}.models.base.Repository",
            "Repository",
        ]

        for repo_qn in repo_qn_patterns:
            create_method = f"{repo_qn}.create"
            if create_method in self.function_registry:
                return cs.TYPE_INFERENCE_BASE_MODEL

        return None

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

    def _traverse_for_assignments_simple(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_ASSIGNMENT:
                self._process_assignment_simple(current, local_var_types, module_qn)

            stack.extend(reversed(current.children))

    def _traverse_for_assignments_complex(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        # (H) DELETE??? Traverse AST for complex assignments (method calls) using existing variable types.
        # (H) NOTE: This is kept for backwards compatibility but _traverse_single_pass
        # (H) should be preferred for better performance.
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_ASSIGNMENT:
                self._process_assignment_complex(current, local_var_types, module_qn)

            stack.extend(reversed(current.children))

    def _process_assignment_simple(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name("left")
        right_node = assignment_node.child_by_field_name("right")

        if not left_node or not right_node:
            return

        var_name = self._extract_variable_name(left_node)
        if not var_name:
            return

        inferred_type = self._infer_type_from_expression_simple(right_node, module_qn)
        if inferred_type:
            local_var_types[var_name] = inferred_type
            logger.debug(lg.PY_TYPE_SIMPLE.format(var=var_name, type=inferred_type))

    def _process_assignment_complex(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name("left")
        right_node = assignment_node.child_by_field_name("right")

        if not left_node or not right_node:
            return

        var_name = self._extract_variable_name(left_node)
        if not var_name:
            return

        if var_name in local_var_types:
            return

        inferred_type = self._infer_type_from_expression_complex(
            right_node, module_qn, local_var_types
        )
        if inferred_type:
            local_var_types[var_name] = inferred_type
            logger.debug(lg.PY_TYPE_COMPLEX.format(var=var_name, type=inferred_type))

    def _process_assignment_for_type_inference(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name("left")
        right_node = assignment_node.child_by_field_name("right")

        if not left_node or not right_node:
            return

        var_name = self._extract_variable_name(left_node)
        if not var_name:
            return

        inferred_type = self._infer_type_from_expression(right_node, module_qn)
        if inferred_type:
            local_var_types[var_name] = inferred_type
            logger.debug(lg.PY_TYPE_INFERRED.format(var=var_name, type=inferred_type))

    def _extract_variable_name(self, node: Node) -> str | None:
        if node.type == cs.TS_PY_IDENTIFIER:
            text = node.text
            if text is not None:
                decoded = safe_decode_text(node)
                if decoded:
                    return decoded
        return None

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

    def _find_method_ast_node(self, method_qn: str) -> Node | None:
        qn_parts = method_qn.split(cs.SEPARATOR_DOT)
        if len(qn_parts) < 3:
            return None

        class_name = qn_parts[-2]
        method_name = qn_parts[-1]

        expected_module = cs.SEPARATOR_DOT.join(qn_parts[:-2])
        if expected_module in self.module_qn_to_file_path:
            file_path = self.module_qn_to_file_path[expected_module]
            if file_path in self.ast_cache:
                root_node, language = self.ast_cache[file_path]
                return self._find_method_in_ast(
                    root_node, class_name, method_name, language
                )

        return None

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
                return self.js_type_inference.find_js_method_in_ast(
                    root_node, class_name, method_name
                )
            case _:
                return None

    def _find_python_method_in_ast(
        self, root_node: Node, class_name: str, method_name: str
    ) -> Node | None:
        lang_queries = self.queries[cs.SupportedLanguage.PYTHON]
        class_query = lang_queries["classes"]
        if not class_query:
            return None
        cursor = QueryCursor(class_query)
        captures = cursor.captures(root_node)

        for class_node in captures.get("class", []):
            if not isinstance(class_node, Node):
                continue

            name_node = class_node.child_by_field_name("name")
            if not name_node:
                continue

            text = name_node.text
            if text is None:
                continue

            found_class_name = safe_decode_text(name_node)
            if found_class_name != class_name:
                continue

            body_node = class_node.child_by_field_name("body")
            method_query = lang_queries["functions"]
            if not body_node or not method_query:
                continue

            method_cursor = QueryCursor(method_query)
            method_captures = method_cursor.captures(body_node)

            for method_node in method_captures.get("function", []):
                if not isinstance(method_node, Node):
                    continue

                method_name_node = method_node.child_by_field_name("name")
                if not method_name_node:
                    continue

                method_text = method_name_node.text
                if method_text is None:
                    continue

                found_method_name = safe_decode_text(method_name_node)
                if found_method_name == method_name:
                    return method_node

        return None

    def _analyze_method_return_statements(
        self, method_node: Node, method_qn: str
    ) -> str | None:
        return_nodes: list[Node] = []
        self._find_return_statements(method_node, return_nodes)

        for return_node in return_nodes:
            return_value = None
            for child in return_node.children:
                if child.type not in (cs.TS_PY_RETURN, cs.TS_PY_KEYWORD):
                    return_value = child
                    break

            if return_value:
                inferred_type = self._analyze_return_expression(return_value, method_qn)
                if inferred_type:
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
        if expr_node.type == cs.TS_PY_CALL:
            func_node = expr_node.child_by_field_name("function")
            if func_node and func_node.type == cs.TS_PY_IDENTIFIER:
                func_text = func_node.text
                if func_text is not None:
                    class_name = safe_decode_text(func_node)
                    if class_name:
                        if class_name == cs.PY_KEYWORD_CLS:
                            qn_parts = method_qn.split(cs.SEPARATOR_DOT)
                            if len(qn_parts) >= 2:
                                return qn_parts[-2]
                        elif (
                            class_name
                            and len(class_name) > 0
                            and class_name[0].isupper()
                        ):
                            module_qn = cs.SEPARATOR_DOT.join(
                                method_qn.split(cs.SEPARATOR_DOT)[:-2]
                            )
                            resolved_class = self._find_class_in_scope(
                                class_name, module_qn
                            )
                            return resolved_class or class_name

            elif func_node and func_node.type == cs.TS_PY_ATTRIBUTE:
                method_call_text = self._extract_full_method_call(func_node)
                if method_call_text:
                    module_qn = cs.SEPARATOR_DOT.join(
                        method_qn.split(cs.SEPARATOR_DOT)[:-2]
                    )
                    return self._infer_method_call_return_type(
                        method_call_text, module_qn
                    )

        elif expr_node.type == cs.TS_PY_IDENTIFIER:
            text = expr_node.text
            if text is not None:
                identifier = safe_decode_text(expr_node)
                if identifier == cs.PY_KEYWORD_SELF or identifier == cs.PY_KEYWORD_CLS:
                    qn_parts = method_qn.split(cs.SEPARATOR_DOT)
                    if len(qn_parts) >= 2:
                        return qn_parts[-2]
                else:
                    module_qn = cs.SEPARATOR_DOT.join(
                        method_qn.split(cs.SEPARATOR_DOT)[:-2]
                    )

                    method_node = self._find_method_ast_node(method_qn)
                    if method_node:
                        local_vars = self.build_local_variable_type_map(
                            method_node, module_qn, cs.SupportedLanguage.PYTHON
                        )
                        if identifier in local_vars:
                            logger.debug(
                                lg.PY_VAR_FROM_CONTEXT.format(
                                    var=identifier, type=local_vars[identifier]
                                )
                            )
                            return local_vars[identifier]

                    logger.debug(lg.PY_VAR_CANNOT_INFER.format(var=identifier))
                    return None

        elif expr_node.type == cs.TS_PY_ATTRIBUTE:
            object_node = expr_node.child_by_field_name("object")
            if object_node and object_node.type == cs.TS_PY_IDENTIFIER:
                object_text = object_node.text
                if object_text is not None:
                    object_name = safe_decode_text(object_node)
                    if (
                        object_name == cs.PY_KEYWORD_CLS
                        or object_name == cs.PY_KEYWORD_SELF
                    ):
                        qn_parts = method_qn.split(cs.SEPARATOR_DOT)
                        if len(qn_parts) >= 2:
                            return qn_parts[-2]

        return None

    def _build_java_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        return self.java_type_inference.build_java_variable_type_map(
            caller_node, module_qn
        )
