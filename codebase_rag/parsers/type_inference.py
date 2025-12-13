import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from .import_processor import ImportProcessor
from .java_type_inference import JavaTypeInferenceEngine
from .js_type_inference import JsTypeInferenceEngine
from .lua_type_inference import LuaTypeInferenceEngine
from .python_utils import resolve_class_name
from .utils import safe_decode_text

if TYPE_CHECKING:
    from .factory import ASTCacheProtocol

_JS_TYPESCRIPT_LANGUAGES = {"javascript", "typescript"}


class TypeInferenceEngine:
    """Handles type inference for local variables and method returns."""

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: Any,
        repo_path: Path,
        project_name: str,
        ast_cache: "ASTCacheProtocol",
        queries: dict[str, Any],
        module_qn_to_file_path: dict[str, Path],
        class_inheritance: dict[str, list[str]],
        simple_name_lookup: dict[str, set[str]],
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

        # Memoization caches to prevent repeated work during recursive type inference
        self._method_return_type_cache: dict[str, str | None] = {}
        # Recursion guard to prevent infinite loops in recursive type inference
        self._type_inference_in_progress: set[str] = set()

    @property
    def java_type_inference(self) -> JavaTypeInferenceEngine:
        """Lazy-loaded Java type inference engine."""
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
        """Lazy-loaded Lua type inference engine."""
        if self._lua_type_inference is None:
            self._lua_type_inference = LuaTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                project_name=self.project_name,
            )
        return self._lua_type_inference

    @property
    def js_type_inference(self) -> JsTypeInferenceEngine:
        """Lazy-loaded JavaScript/TypeScript type inference engine."""
        if self._js_type_inference is None:
            self._js_type_inference = JsTypeInferenceEngine(
                import_processor=self.import_processor,
                function_registry=self.function_registry,
                project_name=self.project_name,
                find_method_ast_node_func=self._find_method_ast_node,
            )
        return self._js_type_inference

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str, language: str
    ) -> dict[str, str]:
        """
        Build a map of local variable names to their inferred types within a function.
        This enables resolution of instance method calls like user.get_name().
        """
        local_var_types: dict[str, str] = {}

        if language == "python":
            pass
        elif language in _JS_TYPESCRIPT_LANGUAGES:
            return self.js_type_inference.build_js_local_variable_type_map(
                caller_node, module_qn, language
            )
        elif language == "java":
            return self.java_type_inference.build_java_variable_type_map(
                caller_node, module_qn
            )
        elif language == "lua":
            return self.lua_type_inference.build_lua_local_variable_type_map(
                caller_node, module_qn
            )
        else:
            return local_var_types

        try:
            self._infer_parameter_types(caller_node, local_var_types, module_qn)

            # Single-pass traversal for all type inference:
            # - Simple assignments (constructors, literals)
            # - Complex assignments (method calls)
            # - Loop variables (comprehensions, for loops)
            # - Instance variables (self.attr)
            # This avoids the previous O(5*N) multiple traversals
            self._traverse_single_pass(caller_node, local_var_types, module_qn)

        except Exception as e:
            logger.debug(f"Failed to build local variable type map: {e}")

        return local_var_types

    def _infer_parameter_types(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Infer types from function parameters when possible."""
        params_node = caller_node.child_by_field_name("parameters")
        if not params_node:
            return

        for param in params_node.children:
            if param.type == "identifier":
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
                                f"Inferred parameter type: {param_name} -> {inferred_type}"
                            )

            elif param.type == "typed_parameter":
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
        """
        Infer a parameter's type by matching its name against available class
        definitions in the current scope (local and imported).
        """
        logger.debug(
            f"Attempting to infer type for parameter '{param_name}' in module '{module_qn}'"
        )
        available_class_names = []

        # 1. Get classes defined in the current module (using trie for O(k) lookup)
        for qn, node_type in self.function_registry.find_with_prefix(module_qn):
            if node_type == "Class":
                # Check if it's directly in this module, not a submodule
                if ".".join(qn.split(".")[:-1]) == module_qn:
                    available_class_names.append(qn.split(".")[-1])

        if module_qn in self.import_processor.import_mapping:
            for local_name, imported_qn in self.import_processor.import_mapping[
                module_qn
            ].items():
                if self.function_registry.get(imported_qn) == "Class":
                    available_class_names.append(local_name)

        logger.debug(f"Available classes in scope: {available_class_names}")

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
            f"Best match for '{param_name}' is '{best_match}' with score {highest_score}"
        )
        return best_match

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        """Convert a simple class name to its fully qualified name."""
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _infer_loop_variable_types(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Infer types from loop variables in comprehensions and for loops."""
        self._find_comprehensions(caller_node, local_var_types, module_qn)
        self._find_for_loops(caller_node, local_var_types, module_qn)

    def _find_comprehensions(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Find and analyze list/dict/set comprehensions."""
        if node.type == "list_comprehension":
            self._analyze_comprehension(node, local_var_types, module_qn)

        for child in node.children:
            self._find_comprehensions(child, local_var_types, module_qn)

    def _find_for_loops(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Find and analyze for loops."""
        if node.type == "for_statement":
            self._analyze_for_loop(node, local_var_types, module_qn)

        for child in node.children:
            self._find_for_loops(child, local_var_types, module_qn)

    def _analyze_comprehension(
        self, comp_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze a comprehension to infer loop variable types."""
        for child in comp_node.children:
            if child.type == "for_in_clause":
                self._analyze_for_in_clause(child, local_var_types, module_qn)

    def _analyze_for_loop(
        self, for_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze a for loop to infer loop variable types."""
        left_node = for_node.child_by_field_name("left")
        right_node = for_node.child_by_field_name("right")

        if left_node and right_node:
            self._infer_loop_var_from_iterable(
                left_node, right_node, local_var_types, module_qn
            )

    def _analyze_for_in_clause(
        self, for_in_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze a for-in clause in a comprehension."""
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
        """Infer loop variable type from the iterable."""
        loop_var = self._extract_variable_name(left_node)
        if not loop_var:
            return

        element_type = self._infer_iterable_element_type(
            right_node, local_var_types, module_qn
        )
        if element_type:
            local_var_types[loop_var] = element_type
            logger.debug(f"Inferred loop variable type: {loop_var} -> {element_type}")

    def _infer_iterable_element_type(
        self, iterable_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """Infer the element type of an iterable."""
        if iterable_node.type == "list":
            return self._infer_list_element_type(
                iterable_node, local_var_types, module_qn
            )

        elif iterable_node.type == "identifier":
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
        """Infer element type from a list literal."""
        for child in list_node.children:
            if child.type == "call":
                func_node = child.child_by_field_name("function")
                if func_node and func_node.type == "identifier":
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
        """Infer types for instance variables by analyzing assignments.

        NOTE: This does a full traversal. For better performance, use
        _infer_instance_variable_types_from_assignments with pre-collected assignments.
        """
        # Look for assignments like self.repo = Repository() in the current method
        self._analyze_self_assignments(caller_node, local_var_types, module_qn)

        self._analyze_class_init_assignments(caller_node, local_var_types, module_qn)

    def _infer_instance_variable_types_from_assignments(
        self, assignments: list[Node], local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Infer types for instance variables from pre-collected assignments.

        This is more efficient than _infer_instance_variable_types as it reuses
        assignments already collected during single-pass traversal.
        """
        for assignment in assignments:
            left_node = assignment.child_by_field_name("left")
            right_node = assignment.child_by_field_name("right")

            if left_node and right_node and left_node.type == "attribute":
                left_text = left_node.text
                if left_text and left_text.decode("utf8").startswith("self."):
                    attr_name = left_text.decode("utf8")
                    assigned_type = self._infer_type_from_expression(
                        right_node, module_qn
                    )
                    if assigned_type:
                        local_var_types[attr_name] = assigned_type
                        logger.debug(
                            f"Inferred instance variable: "
                            f"{attr_name} -> {assigned_type}"
                        )

    def _analyze_class_init_assignments(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze instance variable assignments from the class's __init__ method."""
        class_node = self._find_containing_class(caller_node)
        if not class_node:
            logger.debug("No containing class found for method")
            return

        init_method = self._find_init_method(class_node)
        if not init_method:
            logger.debug("No __init__ method found in class")
            return

        logger.debug("Found __init__ method, analyzing self assignments...")
        self._analyze_self_assignments(init_method, local_var_types, module_qn)

    def _find_containing_class(self, method_node: Node) -> Node | None:
        """Find the class node that contains the given method node."""
        current = method_node.parent
        level = 1
        while current:
            logger.debug(f"Level {level}: node type = {current.type}")
            if current.type == "class_definition":
                logger.debug(f"Found class_definition at level {level}")
                return current
            current = current.parent
            level += 1
            if level > 10:
                break
        logger.debug("No class_definition found in parent hierarchy")
        return None

    def _find_init_method(self, class_node: Node) -> Node | None:
        """Find the __init__ method within a class node."""
        logger.debug(
            f"Searching for __init__ method in class with "
            f"{len(class_node.children)} children"
        )

        class_body = None
        for child in class_node.children:
            logger.debug(f"  Child type: {child.type}")
            if child.type == "block":
                class_body = child
                break

        if not class_body:
            logger.debug("  No class body (block) found")
            return None

        logger.debug(
            f"  Searching in class body with {len(class_body.children)} children"
        )
        for child in class_body.children:
            logger.debug(f"    Body child type: {child.type}")
            if child.type == "function_definition":
                name_node = child.child_by_field_name("name")
                if name_node and name_node.text:
                    method_name = safe_decode_text(name_node)
                    logger.debug(f"      Found method: {method_name}")
                    if method_name == "__init__":
                        logger.debug("      Found __init__ method!")
                        return child
        logger.debug("  No __init__ method found in class body")
        return None

    def _analyze_self_assignments(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze assignments to self.attribute to determine instance variable types."""
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()

            if current.type == "assignment":
                left_node = current.child_by_field_name("left")
                right_node = current.child_by_field_name("right")

                if left_node and right_node and left_node.type == "attribute":
                    left_text = left_node.text
                    left_decoded = safe_decode_text(left_node)
                    if left_text and left_decoded and left_decoded.startswith("self."):
                        attr_name = left_decoded
                        assigned_type = self._infer_type_from_expression(
                            right_node, module_qn
                        )
                        if assigned_type:
                            local_var_types[attr_name] = assigned_type
                            logger.debug(
                                f"Inferred instance variable: "
                                f"{attr_name} -> {assigned_type}"
                            )

            stack.extend(reversed(current.children))

    def _infer_variable_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """Infer element type from a variable that holds a list."""
        if var_name in local_var_types:
            var_type = local_var_types[var_name]
            if var_type and var_type != "list":
                return var_type

        return self._infer_method_return_element_type(
            var_name, local_var_types, module_qn
        )

    def _infer_method_return_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """Infer element type by analyzing method return types."""
        if "all_" in var_name or var_name.endswith("s"):
            return self._analyze_repository_item_type(module_qn)

        return None

    def _analyze_repository_item_type(self, module_qn: str) -> str | None:
        """Analyze Repository class to determine what type of items it stores."""
        repo_qn_patterns = [
            f"{module_qn.split('.')[0]}.models.base.Repository",
            "Repository",
        ]

        for repo_qn in repo_qn_patterns:
            create_method = f"{repo_qn}.create"
            if create_method in self.function_registry:
                return "BaseModel"

        return None

    def _traverse_single_pass(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """
        Single-pass AST traversal for all type inference operations.

        This combines what was previously 5 separate traversals into one:
        - Simple assignments (constructors, literals)
        - Complex assignments (method calls) - done in second phase after first pass
        - Comprehension loop variables
        - For loop variables
        - Instance variables (self.attr)

        Performance: O(N) instead of O(5*N) where N = AST size.
        """
        # Collect assignments during first pass for two-phase processing
        assignments: list[Node] = []
        comprehensions: list[Node] = []
        for_statements: list[Node] = []

        # Single traversal to collect all relevant nodes
        stack: list[Node] = [node]
        while stack:
            current = stack.pop()
            node_type = current.type

            if node_type == "assignment":
                assignments.append(current)
            elif node_type == "list_comprehension":
                comprehensions.append(current)
            elif node_type == "for_statement":
                for_statements.append(current)

            stack.extend(reversed(current.children))

        # Phase 1: Process simple assignments first (constructors, literals)
        for assignment in assignments:
            self._process_assignment_simple(assignment, local_var_types, module_qn)

        # Phase 2: Process complex assignments using types from phase 1
        for assignment in assignments:
            self._process_assignment_complex(assignment, local_var_types, module_qn)

        # Phase 3: Process comprehensions
        for comp in comprehensions:
            self._analyze_comprehension(comp, local_var_types, module_qn)

        # Phase 4: Process for loops
        for for_stmt in for_statements:
            self._analyze_for_loop(for_stmt, local_var_types, module_qn)

        # Phase 5: Instance variables (self.attr) - reuses the assignments collected
        self._infer_instance_variable_types_from_assignments(
            assignments, local_var_types, module_qn
        )

    def _traverse_for_assignments_simple(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Traverse AST for simple assignments (constructors, literals) only.

        NOTE: This is kept for backwards compatibility but _traverse_single_pass
        should be preferred for better performance.
        """
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()
            if current.type == "assignment":
                self._process_assignment_simple(current, local_var_types, module_qn)

            stack.extend(reversed(current.children))

    def _traverse_for_assignments_complex(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Traverse AST for complex assignments (method calls) using existing variable types.

        NOTE: This is kept for backwards compatibility but _traverse_single_pass
        should be preferred for better performance.
        """
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()
            if current.type == "assignment":
                self._process_assignment_complex(current, local_var_types, module_qn)

            stack.extend(reversed(current.children))

    def _process_assignment_simple(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process simple assignments (constructors, literals) to infer variable types."""
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
            logger.debug(f"Inferred type (simple): {var_name} -> {inferred_type}")

    def _process_assignment_complex(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process complex assignments (method calls) using existing variable types."""
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
            logger.debug(f"Inferred type (complex): {var_name} -> {inferred_type}")

    def _process_assignment_for_type_inference(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process an assignment node to infer variable types."""
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
            logger.debug(f"Inferred type: {var_name} -> {inferred_type}")

    def _extract_variable_name(self, node: Node) -> str | None:
        """Extract variable name from assignment left side (handles simple cases)."""
        if node.type == "identifier":
            text = node.text
            if text is not None:
                decoded = safe_decode_text(node)
                if decoded:
                    result: str = decoded
                    return result
        return None

    def _infer_type_from_expression(self, node: Node, module_qn: str) -> str | None:
        """Infer type from the right-hand side of an assignment."""
        if node.type == "call":
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == "identifier":
                func_text = func_node.text
                if func_text is not None:
                    class_name = safe_decode_text(func_node)
                    if class_name and len(class_name) > 0 and class_name[0].isupper():
                        return class_name

            elif func_node and func_node.type == "attribute":
                method_call_text = self._extract_full_method_call(func_node)
                if method_call_text:
                    return self._infer_method_call_return_type(
                        method_call_text, module_qn, local_var_types=None
                    )

        elif node.type == "list_comprehension":
            body_node = node.child_by_field_name("body")
            if body_node:
                return self._infer_type_from_expression(body_node, module_qn)

        return None

    def _infer_type_from_expression_simple(
        self, node: Node, module_qn: str
    ) -> str | None:
        """Infer type from simple expressions (constructors, literals) only."""
        if node.type == "call":
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == "identifier":
                func_text = func_node.text
                if func_text is not None:
                    class_name = safe_decode_text(func_node)
                    if class_name and len(class_name) > 0 and class_name[0].isupper():
                        return class_name

        elif node.type == "list_comprehension":
            body_node = node.child_by_field_name("body")
            if body_node:
                return self._infer_type_from_expression_simple(body_node, module_qn)

        return None

    def _infer_type_from_expression_complex(
        self, node: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> str | None:
        """Infer type from complex expressions (method calls) using existing variable types."""
        if node.type == "call":
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == "attribute":
                method_call_text = self._extract_full_method_call(func_node)
                if method_call_text:
                    return self._infer_method_call_return_type(
                        method_call_text, module_qn, local_var_types
                    )

        return None

    def _extract_full_method_call(self, attr_node: Node) -> str | None:
        """Extract the full method call text from an attribute node."""
        if attr_node.text:
            decoded = safe_decode_text(attr_node)
            if decoded:
                result: str = decoded
                return result
        return None

    def _infer_method_call_return_type(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """Infer return type of a method call via static analysis."""
        # Create a cache key for this specific method call + context
        cache_key = f"{module_qn}:{method_call}"

        # Recursion guard: if we're already inferring this call's type, return None
        if cache_key in self._type_inference_in_progress:
            logger.debug(f"Recursion guard (method call): skipping {method_call}")
            return None

        self._type_inference_in_progress.add(cache_key)
        try:
            # Handle chained method calls first
            if "." in method_call and self._is_method_chain(method_call):
                return self._infer_chained_call_return_type_fixed(
                    method_call, module_qn, local_var_types
                )

            # Try proper AST analysis for non-chained calls
            return self._infer_method_return_type(method_call, module_qn, local_var_types)
        finally:
            self._type_inference_in_progress.discard(cache_key)

    def _is_method_chain(self, call_name: str) -> bool:
        """Check if this appears to be a method chain with parentheses."""
        if "(" in call_name and ")" in call_name:
            return bool(re.search(r"\)\.[^)]*$", call_name))
        return False

    def _infer_chained_call_return_type_fixed(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """Infer return type for chained method calls like obj.method().other_method()."""
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
            if "." not in object_type:
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
        """Infer the type of an object expression for chained calls."""
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
        """Infer return type for chained method calls like obj.method().other_method()."""
        return self._infer_chained_call_return_type_fixed(
            call_name, module_qn, local_var_types
        )

    def _infer_expression_return_type(
        self,
        expression: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """Infer the return type of a complex expression like 'user.method(args)'."""
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
        """Get method return type by analyzing its AST implementation."""
        # Check memoization cache first
        if method_qn in self._method_return_type_cache:
            return self._method_return_type_cache[method_qn]

        # Recursion guard: if we're already inferring this method's type, return None
        # This prevents infinite loops in recursive type inference chains
        if method_qn in self._type_inference_in_progress:
            logger.debug(f"Recursion guard: skipping {method_qn}")
            return None

        # Mark as in-progress
        self._type_inference_in_progress.add(method_qn)
        try:
            # Find the method's AST node from our cache
            method_node = self._find_method_ast_node(method_qn)
            if not method_node:
                result = None
            else:
                # Analyze return statements in the method
                result = self._analyze_method_return_statements(method_node, method_qn)

            # Cache the result
            self._method_return_type_cache[method_qn] = result
            return result
        finally:
            # Remove from in-progress set
            self._type_inference_in_progress.discard(method_qn)

    def _extract_object_type_from_call(
        self,
        object_part: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """Extract the type of an object from a method call."""
        if local_var_types and object_part in local_var_types:
            return local_var_types[object_part]

        return None

    def _infer_method_return_type(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """
        Infer the return type of a method call by analyzing the method's implementation.
        """
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
            logger.debug(f"Failed to infer return type for {method_call}: {e}")
            return None

    def _resolve_method_qualified_name(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """Resolve a method call like 'self.manager.create_user' to its qualified name."""
        if "." not in method_call:
            return None

        parts = method_call.split(".")
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

        if parts[0] == "self" and len(parts) >= 3:
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
        """Resolve a method on a specific class."""
        local_class_qn = f"{module_qn}.{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == "Class"
        ):
            method_qn = f"{local_class_qn}.{method_name}"
            if (
                method_qn in self.function_registry
                and self.function_registry[method_qn] == "Method"
            ):
                return method_qn

        if module_qn in self.import_processor.import_mapping:
            import_mapping = self.import_processor.import_mapping[module_qn]

            if class_name in import_mapping:
                imported_class_qn = import_mapping[class_name]
                if (
                    imported_class_qn in self.function_registry
                    and self.function_registry[imported_class_qn] == "Class"
                ):
                    method_qn = f"{imported_class_qn}.{method_name}"
                    if (
                        method_qn in self.function_registry
                        and self.function_registry[method_qn] == "Method"
                    ):
                        return method_qn

        # Search through all known classes with matching names (using simple_name_lookup for O(1))
        if class_name in self.simple_name_lookup:
            for qn in self.simple_name_lookup[class_name]:
                if self.function_registry.get(qn) == "Class":
                    method_qn = f"{qn}.{method_name}"
                    if (
                        method_qn in self.function_registry
                        and self.function_registry[method_qn] == "Method"
                    ):
                        logger.debug(f"Resolved {class_name}.{method_name} to {method_qn}")
                        return method_qn

        return None

    def _infer_attribute_type(self, attribute_name: str, module_qn: str) -> str | None:
        """Infer the type of an instance attribute like self.manager."""

        try:
            # Use module_qn_to_file_path for O(1) lookup instead of iterating all files
            if module_qn in self.module_qn_to_file_path:
                file_path = self.module_qn_to_file_path[module_qn]
                if file_path in self.ast_cache:
                    root_node, language = self.ast_cache[file_path]
                    if language == "python":
                        # Look for all classes in this module and analyze their instance variables
                        instance_vars: dict[str, str] = {}
                        self._analyze_self_assignments(root_node, instance_vars, module_qn)

                        # Check if our attribute was found
                        full_attr_name = f"self.{attribute_name}"
                        if full_attr_name in instance_vars:
                            attr_type: str = instance_vars[full_attr_name]
                            return attr_type

        except Exception as e:
            logger.debug(
                f"Failed to analyze instance variables for {attribute_name}: {e}"
            )

        if "_" in attribute_name:
            parts = attribute_name.split("_")
            class_name = "".join(word.capitalize() for word in parts)
        else:
            class_name = attribute_name.capitalize()

        return self._find_class_in_scope(class_name, module_qn)

    def _find_class_in_scope(self, class_name: str, module_qn: str) -> str | None:
        """Find a class by name in the current module's scope."""
        local_class_qn = f"{module_qn}.{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == "Class"
        ):
            return class_name

        if module_qn in self.import_processor.import_mapping:
            import_mapping = self.import_processor.import_mapping[module_qn]
            for local_name, imported_qn in import_mapping.items():
                if (
                    local_name == class_name
                    and imported_qn in self.function_registry
                    and self.function_registry[imported_qn] == "Class"
                ):
                    return class_name

        # Look for classes with matching simple names across the project (using simple_name_lookup for O(1))
        if class_name in self.simple_name_lookup:
            for qn in self.simple_name_lookup[class_name]:
                if self.function_registry.get(qn) == "Class":
                    return class_name

        return None

    def _find_method_ast_node(self, method_qn: str) -> Node | None:
        """Find the AST node for a method by its qualified name."""
        qn_parts = method_qn.split(".")
        if len(qn_parts) < 3:
            return None

        project_name = qn_parts[0]
        class_name = qn_parts[-2]
        method_name = qn_parts[-1]

        # Use module_qn_to_file_path for O(1) lookup instead of iterating all files
        expected_module = ".".join(qn_parts[:-2])  # Remove class and method name
        if expected_module in self.module_qn_to_file_path:
            file_path = self.module_qn_to_file_path[expected_module]
            if file_path in self.ast_cache:
                root_node, language = self.ast_cache[file_path]
                return self._find_method_in_ast(
                    root_node, class_name, method_name, language
                )

        return None

    def _find_method_in_ast(
        self, root_node: Node, class_name: str, method_name: str, language: str
    ) -> Node | None:
        """Find a specific method within a class in the AST."""
        if language == "python":
            return self._find_python_method_in_ast(root_node, class_name, method_name)
        elif language in ("javascript", "typescript"):
            return self.js_type_inference.find_js_method_in_ast(
                root_node, class_name, method_name
            )
        return None

    def _find_python_method_in_ast(
        self, root_node: Node, class_name: str, method_name: str
    ) -> Node | None:
        """Find a specific method within a Python class in the AST."""
        lang_queries = self.queries["python"]
        class_query = lang_queries["classes"]
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
            if not body_node:
                continue

            method_query = lang_queries["functions"]
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
        """Analyze return statements in a method to infer return type."""
        return_nodes: list[Node] = []
        self._find_return_statements(method_node, return_nodes)

        for return_node in return_nodes:
            return_value = None
            for child in return_node.children:
                if child.type not in ["return", "keyword"]:
                    return_value = child
                    break

            if return_value:
                inferred_type = self._analyze_return_expression(return_value, method_qn)
                if inferred_type:
                    return inferred_type

        return None

    def _find_return_statements(self, node: Node, return_nodes: list[Node]) -> None:
        """Collect all return statements in a node using iterative traversal."""
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()
            if current.type == "return_statement":
                return_nodes.append(current)

            stack.extend(reversed(current.children))

    def _analyze_return_expression(self, expr_node: Node, method_qn: str) -> str | None:
        """Analyze a return expression to infer its type."""
        if expr_node.type == "call":
            func_node = expr_node.child_by_field_name("function")
            if func_node and func_node.type == "identifier":
                func_text = func_node.text
                if func_text is not None:
                    class_name = safe_decode_text(func_node)
                    if class_name:
                        if class_name == "cls":
                            qn_parts = method_qn.split(".")
                            if len(qn_parts) >= 2:
                                return qn_parts[-2]
                        elif (
                            class_name
                            and len(class_name) > 0
                            and class_name[0].isupper()
                        ):
                            module_qn = ".".join(method_qn.split(".")[:-2])
                            resolved_class = self._find_class_in_scope(
                                class_name, module_qn
                            )
                            return resolved_class or class_name

            elif func_node and func_node.type == "attribute":
                method_call_text = self._extract_full_method_call(func_node)
                if method_call_text:
                    module_qn = ".".join(method_qn.split(".")[:-2])
                    return self._infer_method_call_return_type(
                        method_call_text, module_qn
                    )

        elif expr_node.type == "identifier":
            text = expr_node.text
            if text is not None:
                identifier = safe_decode_text(expr_node)
                if identifier == "self" or identifier == "cls":
                    qn_parts = method_qn.split(".")
                    if len(qn_parts) >= 2:
                        return qn_parts[-2]
                else:
                    module_qn = ".".join(method_qn.split(".")[:-2])

                    method_node = self._find_method_ast_node(method_qn)
                    if method_node:
                        local_vars = self.build_local_variable_type_map(
                            method_node, module_qn, "python"
                        )
                        if identifier in local_vars:
                            logger.debug(
                                f"Found variable type from method context: {identifier} -> {local_vars[identifier]}"
                            )
                            return local_vars[identifier]

                    logger.debug(
                        f"Cannot infer type for variable reference: {identifier}"
                    )
                    return None

        elif expr_node.type == "attribute":
            object_node = expr_node.child_by_field_name("object")
            if object_node and object_node.type == "identifier":
                object_text = object_node.text
                if object_text is not None:
                    object_name = safe_decode_text(object_node)
                    if object_name == "cls" or object_name == "self":
                        qn_parts = method_qn.split(".")
                        if len(qn_parts) >= 2:
                            return qn_parts[-2]

        return None

    def _build_java_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        """Build local variable type map for Java using JavaTypeInferenceEngine."""
        return self.java_type_inference.build_java_variable_type_map(
            caller_node, module_qn
        )
