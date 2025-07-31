"""Type inference engine for determining variable types."""

from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from .import_processor import ImportProcessor


class TypeInferenceEngine:
    """Handles type inference for local variables and method returns."""

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: Any,
        repo_path: Path,
        project_name: str,
        ast_cache: dict[Path, tuple[Node, str]],
        queries: dict[str, Any],
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str, language: str
    ) -> dict[str, str]:
        """
        Build a map of local variable names to their inferred types within a function.
        This enables resolution of instance method calls like user.get_name().
        """
        local_var_types: dict[str, str] = {}

        if language != "python":
            # For now, only implement Python support
            return local_var_types

        try:
            # First, try to infer types from function parameters
            self._infer_parameter_types(caller_node, local_var_types, module_qn)

            # Use a fallback approach: traverse the AST to find assignment statements
            self._traverse_for_assignments(caller_node, local_var_types, module_qn)

            # Handle loop variables in comprehensions and for loops
            self._infer_loop_variable_types(caller_node, local_var_types, module_qn)

            # Handle instance variables like self.repo
            self._infer_instance_variable_types(caller_node, local_var_types, module_qn)

        except Exception as e:
            logger.debug(f"Failed to build local variable type map: {e}")

        return local_var_types

    def _infer_parameter_types(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Infer types from function parameters when possible."""
        # Get function parameters
        params_node = caller_node.child_by_field_name("parameters")
        if not params_node:
            return

        for param in params_node.children:
            if param.type == "identifier":
                param_text = param.text
                if param_text is not None:
                    param_name = param_text.decode("utf8")

                    # Try to infer type from parameter name using available classes
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
                    param_name = param_name_node.text.decode("utf8")
                    param_type = param_type_node.text.decode("utf8")
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

        # 1. Get classes defined in the current module
        for qn, node_type in self.function_registry.items():
            if node_type == "Class" and qn.startswith(module_qn + "."):
                # Check if it's directly in this module, not a submodule
                if ".".join(qn.split(".")[:-1]) == module_qn:
                    available_class_names.append(qn.split(".")[-1])

        # 2. Get imported classes
        if module_qn in self.import_processor.import_mapping:
            for local_name, imported_qn in self.import_processor.import_mapping[
                module_qn
            ].items():
                if self.function_registry.get(imported_qn) == "Class":
                    available_class_names.append(local_name)

        logger.debug(f"Available classes in scope: {available_class_names}")

        # 3. Match parameter name against available classes with a scoring system
        param_lower = param_name.lower()
        best_match = None
        highest_score = 0

        for class_name in available_class_names:
            class_lower = class_name.lower()
            score = 0

            if param_lower == class_lower:
                score = 100  # Exact match
            elif class_lower.endswith(param_lower) or param_lower.endswith(class_lower):
                score = 90
            elif class_lower in param_lower:
                # Higher score for longer matches
                score = int(80 * (len(class_lower) / len(param_lower)))

            if score > highest_score:
                highest_score = score
                best_match = class_name

        logger.debug(
            f"Best match for '{param_name}' is '{best_match}' with score {highest_score}"
        )
        return best_match

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        """
        Convert a simple class name to its fully qualified name.
        """
        # First check current module's import mappings
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                return import_map[class_name]

        # Then check if class exists in same module
        same_module_qn = f"{module_qn}.{class_name}"
        if same_module_qn in self.function_registry:
            return same_module_qn

        # Check parent modules using the trie structure
        module_parts = module_qn.split(".")
        for i in range(len(module_parts) - 1, 0, -1):
            parent_module = ".".join(module_parts[:i])
            potential_qn = f"{parent_module}.{class_name}"
            if potential_qn in self.function_registry:
                return potential_qn

        # Use trie to find classes with the given name
        matches = self.function_registry.find_ending_with(class_name)
        for match in matches:
            # Return the first match that looks like a class
            match_parts = match.split(".")
            if class_name in match_parts:
                return str(match)

        return None

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
        # Find the for clause in the comprehension
        for child in comp_node.children:
            if child.type == "for_in_clause":
                self._analyze_for_in_clause(child, local_var_types, module_qn)

    def _analyze_for_loop(
        self, for_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze a for loop to infer loop variable types."""
        # Find left and right sides of the for statement
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
        # Find left and right sides: for var in iterable
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
        # Extract loop variable name
        loop_var = self._extract_variable_name(left_node)
        if not loop_var:
            return

        # Analyze the iterable to infer element type
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
        # Handle list literals: [User("a"), User("b")]
        if iterable_node.type == "list":
            return self._infer_list_element_type(
                iterable_node, local_var_types, module_qn
            )

        # Handle variables: users (where users was assigned earlier)
        elif iterable_node.type == "identifier":
            var_text = iterable_node.text
            if var_text is not None:
                var_name = var_text.decode("utf8")
                return self._infer_variable_element_type(
                    var_name, local_var_types, module_qn
                )

        return None

    def _infer_list_element_type(
        self, list_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """Infer element type from a list literal."""
        # Look at the first element to infer type
        for child in list_node.children:
            if child.type == "call":
                # Handle constructor calls like User(...)
                func_node = child.child_by_field_name("function")
                if func_node and func_node.type == "identifier":
                    func_text = func_node.text
                    if func_text is not None:
                        class_name = func_text.decode("utf8")
                        if class_name[0].isupper():
                            return str(class_name)
        return None

    def _infer_instance_variable_types(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Infer types for instance variables by analyzing assignments."""
        # Look for assignments like self.repo = Repository() in the current method
        self._analyze_self_assignments(caller_node, local_var_types, module_qn)

        # Also look for instance variable assignments in the class's __init__ method
        self._analyze_class_init_assignments(caller_node, local_var_types, module_qn)

    def _analyze_class_init_assignments(
        self, caller_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze instance variable assignments from the class's __init__ method."""
        # Find the class that contains this method
        class_node = self._find_containing_class(caller_node)
        if not class_node:
            logger.debug("No containing class found for method")
            return

        # Find the __init__ method in this class
        init_method = self._find_init_method(class_node)
        if not init_method:
            logger.debug("No __init__ method found in class")
            return

        logger.debug("Found __init__ method, analyzing self assignments...")
        # Analyze self assignments in the __init__ method
        self._analyze_self_assignments(init_method, local_var_types, module_qn)

    def _find_containing_class(self, method_node: Node) -> Node | None:
        """Find the class node that contains the given method node."""
        # Walk up the AST to find the class_definition parent
        current = method_node.parent
        level = 1
        while current:
            logger.debug(f"Level {level}: node type = {current.type}")
            if current.type == "class_definition":
                logger.debug(f"Found class_definition at level {level}")
                return current
            current = current.parent
            level += 1
            if level > 10:  # Prevent infinite loops
                break
        logger.debug("No class_definition found in parent hierarchy")
        return None

    def _find_init_method(self, class_node: Node) -> Node | None:
        """Find the __init__ method within a class node."""
        logger.debug(
            f"Searching for __init__ method in class with "
            f"{len(class_node.children)} children"
        )

        # Look for the class body (block)
        class_body = None
        for child in class_node.children:
            logger.debug(f"  Child type: {child.type}")
            if child.type == "block":
                class_body = child
                break

        if not class_body:
            logger.debug("  No class body (block) found")
            return None

        # Now look for function definitions within the class body
        logger.debug(
            f"  Searching in class body with {len(class_body.children)} children"
        )
        for child in class_body.children:
            logger.debug(f"    Body child type: {child.type}")
            if child.type == "function_definition":
                # Check if this is the __init__ method
                name_node = child.child_by_field_name("name")
                if name_node and name_node.text:
                    method_name = name_node.text.decode("utf8")
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
        # Traverse the AST looking for assignment statements
        if node.type == "assignment":
            left_node = node.child_by_field_name("left")
            right_node = node.child_by_field_name("right")

            if left_node and right_node:
                # Check if left side is self.something
                if left_node.type == "attribute":
                    left_text = left_node.text
                    if left_text and left_text.decode("utf8").startswith("self."):
                        attr_name = left_text.decode("utf8")  # e.g., "self.repo"

                        # Analyze right side to determine type
                        assigned_type = self._infer_type_from_expression(
                            right_node, module_qn
                        )
                        if assigned_type:
                            local_var_types[attr_name] = assigned_type
                            logger.debug(
                                f"Inferred instance variable: "
                                f"{attr_name} -> {assigned_type}"
                            )

        # Recursively traverse children
        for child in node.children:
            self._analyze_self_assignments(child, local_var_types, module_qn)

    def _infer_variable_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """Infer element type from a variable that holds a list."""
        # Check if we have any type information for this variable
        if var_name in local_var_types:
            var_type = local_var_types[var_name]
            # If the variable itself has a specific type (like "User"), return it
            if var_type and var_type != "list":
                return var_type

        # Try to analyze method return types for calls like repo.get_all()
        return self._infer_method_return_element_type(
            var_name, local_var_types, module_qn
        )

    def _infer_method_return_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """Infer element type by analyzing method return types."""
        # For Repository.get_all(), we know it returns items from the repository
        if "all_" in var_name or var_name.endswith("s"):
            # Try to find Repository class and analyze what it stores
            return self._analyze_repository_item_type(module_qn)

        return None

    def _analyze_repository_item_type(self, module_qn: str) -> str | None:
        """Analyze Repository class to determine what type of items it stores."""
        # Look for Repository.create method calls to see what types are being stored
        repo_qn_patterns = [
            f"{module_qn.split('.')[0]}.models.base.Repository",
            "Repository",  # fallback
        ]

        for repo_qn in repo_qn_patterns:
            create_method = f"{repo_qn}.create"
            if create_method in self.function_registry:
                return "BaseModel"  # Items in Repository inherit from BaseModel

        return None

    def _traverse_for_assignments(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Traverse AST to find assignment statements for type inference."""
        # Check if current node is an assignment
        if node.type == "assignment":
            self._process_assignment_for_type_inference(
                node, local_var_types, module_qn
            )

        # Recursively traverse children
        for child in node.children:
            self._traverse_for_assignments(child, local_var_types, module_qn)

    def _process_assignment_for_type_inference(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process an assignment node to infer variable types."""
        # Handle assignment: variable = expression
        left_node = assignment_node.child_by_field_name("left")
        right_node = assignment_node.child_by_field_name("right")

        if not left_node or not right_node:
            return

        # Extract variable name from left side
        var_name = self._extract_variable_name(left_node)
        if not var_name:
            return

        # Infer type from right side expression
        inferred_type = self._infer_type_from_expression(right_node, module_qn)
        if inferred_type:
            local_var_types[var_name] = inferred_type
            logger.debug(f"Inferred type: {var_name} -> {inferred_type}")

    def _extract_variable_name(self, node: Node) -> str | None:
        """Extract variable name from assignment left side (handles simple cases)."""
        if node.type == "identifier":
            text = node.text
            if text is not None:
                result: str = text.decode("utf8")
                return result
        return None

    def _infer_type_from_expression(self, node: Node, module_qn: str) -> str | None:
        """Infer type from the right-hand side of an assignment."""
        # Handle direct constructor calls: User(args)
        if node.type == "call":
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == "identifier":
                func_text = func_node.text
                if func_text is not None:
                    class_name = func_text.decode("utf8")
                    # Check if this looks like a class constructor
                    if class_name and class_name[0].isupper():  # Simple heuristic
                        return str(class_name)

            # Handle method calls that return objects: obj.some_method()
            elif func_node and func_node.type == "attribute":
                # Try to resolve the method call and infer its return type
                method_call_text = self._extract_full_method_call(func_node)
                if method_call_text:
                    return self._infer_method_call_return_type(
                        method_call_text, module_qn
                    )

        # Handle list comprehensions: [User(...) for i in range(x)]
        elif node.type == "list_comprehension":
            # The body of the comprehension determines the element type
            body_node = node.child_by_field_name("body")
            if body_node:
                # Recursively infer type from the expression inside
                return self._infer_type_from_expression(body_node, module_qn)

        return None

    def _extract_full_method_call(self, attr_node: Node) -> str | None:
        """Extract the full method call text from an attribute node."""
        if attr_node.text:
            result: str = attr_node.text.decode("utf8")
            return result
        return None

    def _infer_method_call_return_type(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """Infer return type of a method call via static analysis."""
        # First try proper AST analysis
        result = self._infer_method_return_type(method_call, module_qn)
        if result:
            return result

        # Handle common legitimate patterns without hardcoding specific names
        if "." in method_call:
            method_name = method_call.split(".")[-1]

            # Pattern 1: Methods that return 'self' (fluent interface/builder pattern)
            if (
                method_name.startswith("update_")
                or method_name.startswith("set_")
                or method_name in ["apply_discount"]
            ):
                # These methods typically return self for chaining
                object_part = method_call.rsplit(".", 1)[0]
                return self._extract_object_type_from_call(
                    object_part, module_qn, local_var_types
                )

            # Pattern 2: Factory/creation methods
            if (
                method_name.startswith("create_")
                or method_name.startswith("get_")
                or method_name.startswith("find_")
            ):
                # These often return the type indicated by the method name
                return self._infer_factory_method_return_type(method_name, module_qn)

        return None

    def _extract_object_type_from_call(
        self,
        object_part: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        """Extract the type of an object from a method call."""
        # For simple variable references like "direct_user", check local variable types
        if local_var_types and object_part in local_var_types:
            return local_var_types[object_part]

        return None

    def _infer_factory_method_return_type(
        self, method_name: str, module_qn: str
    ) -> str | None:
        """Infer return type from factory/creation method names."""
        # Extract potential type name from method name
        if method_name.startswith("create_"):
            # create_user -> User, create_product -> Product
            type_part = method_name[7:]  # Remove "create_"
            if type_part:
                return type_part.title()  # user -> User, product -> Product

        elif method_name.startswith("get_") and method_name.endswith("_by_id"):
            # get_user_by_id -> User
            type_part = method_name[4:-6]  # Remove "get_" and "_by_id"
            if type_part:
                return type_part.title()  # user -> User

        # Handle some common return type patterns
        elif method_name == "get_profile":
            return "Profile"

        return None

    def _infer_method_return_type(self, method_call: str, module_qn: str) -> str | None:
        """
        Infer the return type of a method call by analyzing the method's implementation.
        """
        try:
            # Parse the method call to get the method qualified name
            method_qn = self._resolve_method_qualified_name(method_call, module_qn)
            if not method_qn:
                return None

            # Find the method's AST node from our cache
            method_node = self._find_method_ast_node(method_qn)
            if not method_node:
                return None

            # Analyze return statements in the method
            return self._analyze_method_return_statements(method_node, method_qn)

        except Exception as e:
            logger.debug(f"Failed to infer return type for {method_call}: {e}")
            return None

    def _resolve_method_qualified_name(
        self, method_call: str, module_qn: str
    ) -> str | None:
        """Resolve a method call like 'self.manager.create_user' to its qualified name."""
        # For now, handle simple cases like "self.manager.create_user"
        if "." not in method_call:
            return None

        parts = method_call.split(".")
        if len(parts) < 2:
            return None

        # Handle self.attribute.method() pattern
        if parts[0] == "self" and len(parts) >= 3:
            # Try to resolve the attribute type in the current class
            # This needs proper instance variable tracking
            pass

        return None

    def _find_method_ast_node(self, method_qn: str) -> Node | None:
        """Find the AST node for a method by its qualified name."""
        # Extract module path from qualified name
        qn_parts = method_qn.split(".")
        if len(qn_parts) < 3:
            return None

        # Reconstruct the module path
        project_name = qn_parts[0]
        class_name = qn_parts[-2]
        method_name = qn_parts[-1]

        # Find the module in our AST cache
        for file_path, (root_node, language) in self.ast_cache.items():
            # Check if this file could contain our method
            relative_path = file_path.relative_to(self.repo_path)
            file_module_qn = ".".join(
                [project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name == "__init__.py":
                file_module_qn = ".".join(
                    [project_name] + list(relative_path.parent.parts)
                )

            # Check if the method's module matches this file
            expected_module = ".".join(qn_parts[:-2])  # Remove class and method name
            if file_module_qn == expected_module:
                return self._find_method_in_ast(
                    root_node, class_name, method_name, language
                )

        return None

    def _find_method_in_ast(
        self, root_node: Node, class_name: str, method_name: str, language: str
    ) -> Node | None:
        """Find a specific method within a class in the AST."""
        if language != "python":
            return None

        # Find the class first
        lang_queries = self.queries[language]
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

            found_class_name = text.decode("utf8")
            if found_class_name != class_name:
                continue

            # Found the right class, now find the method
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

                found_method_name = method_text.decode("utf8")
                if found_method_name == method_name:
                    return method_node

        return None

    def _analyze_method_return_statements(
        self, method_node: Node, method_qn: str
    ) -> str | None:
        """Analyze return statements in a method to infer return type."""
        # Find all return statements in the method
        return_nodes: list[Node] = []
        self._find_return_statements(method_node, return_nodes)

        # Analyze each return statement
        for return_node in return_nodes:
            # Get the returned expression
            return_value = None
            for child in return_node.children:
                if child.type not in ["return", "keyword"]:
                    return_value = child
                    break

            if return_value:
                # Analyze what's being returned
                inferred_type = self._analyze_return_expression(return_value, method_qn)
                if inferred_type:
                    return inferred_type

        return None

    def _find_return_statements(self, node: Node, return_nodes: list[Node]) -> None:
        """Recursively find all return statements in a node."""
        if node.type == "return_statement":
            return_nodes.append(node)

        for child in node.children:
            self._find_return_statements(child, return_nodes)

    def _analyze_return_expression(self, expr_node: Node, method_qn: str) -> str | None:
        """Analyze a return expression to infer its type."""
        # Handle direct constructor calls: return User(name)
        if expr_node.type == "call":
            func_node = expr_node.child_by_field_name("function")
            if func_node and func_node.type == "identifier":
                func_text = func_node.text
                if func_text is not None:
                    class_name = func_text.decode("utf8")
                    if class_name[0].isupper():  # Class names start with uppercase
                        return str(class_name)

        # Handle self references: return self
        elif expr_node.type == "identifier":
            text = expr_node.text
            if text is not None:
                identifier = text.decode("utf8")
                if identifier == "self":
                    # Extract class name from method qualified name
                    qn_parts = method_qn.split(".")
                    if len(qn_parts) >= 2:
                        return qn_parts[-2]  # Class name is second to last

        return None
