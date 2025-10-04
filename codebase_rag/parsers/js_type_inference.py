"""JavaScript/TypeScript-specific type inference engine using tree-sitter."""

from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node

from .import_processor import ImportProcessor

if TYPE_CHECKING:
    pass


class JsTypeInferenceEngine:
    """Handles precise type inference for JavaScript/TypeScript using tree-sitter AST analysis."""

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: Any,
        project_name: str,
        find_method_ast_node_func: Any,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name
        self._find_method_ast_node = find_method_ast_node_func

    def build_js_local_variable_type_map(
        self, caller_node: Node, module_qn: str, language: str
    ) -> dict[str, str]:
        """Build local variable type map for JavaScript/TypeScript using stack-based traversal."""
        local_var_types: dict[str, str] = {}

        # Use stack-based traversal to find ALL variable declarations in the caller's scope
        # This includes method-scoped variables that tree-sitter locals query misses
        stack: list[Node] = [caller_node]

        declarator_count = 0

        while stack:
            current = stack.pop()

            # Look for variable declarations: const storage = Storage.getInstance()
            if current.type == "variable_declarator":
                declarator_count += 1
                name_node = current.child_by_field_name("name")
                value_node = current.child_by_field_name("value")

                if name_node and value_node:
                    var_name_text = name_node.text
                    if var_name_text:
                        var_name = var_name_text.decode("utf8")
                        logger.debug(
                            f"Found variable declarator: {var_name} in {module_qn}"
                        )

                        # Infer the type from the value expression
                        var_type = self._infer_js_variable_type_from_value(
                            value_node, module_qn
                        )
                        if var_type:
                            local_var_types[var_name] = var_type
                            logger.debug(
                                f"Inferred JS variable: {var_name} -> {var_type}"
                            )
                        else:
                            logger.debug(
                                f"Could not infer type for variable: {var_name}"
                            )

            # Queue children for traversal (reversed to maintain order)
            stack.extend(reversed(current.children))

        logger.debug(
            f"Built JS variable type map with {len(local_var_types)} variables "
            f"(found {declarator_count} declarators total)"
        )
        return local_var_types

    def _infer_js_variable_type_from_value(
        self, value_node: Node, module_qn: str
    ) -> str | None:
        """Infer the type of a JavaScript variable from its value expression."""
        logger.debug(f"Inferring type from value node type: {value_node.type}")

        # Look for patterns like: const animal = new Animal(...)
        if value_node.type == "new_expression":
            # Extract the constructor name
            constructor_node = value_node.child_by_field_name("constructor")
            if constructor_node and constructor_node.type == "identifier":
                constructor_name = constructor_node.text
                if constructor_name:
                    class_name = str(constructor_node.text.decode("utf8"))
                    # Resolve to fully qualified name
                    class_qn = self._resolve_js_class_name(class_name, module_qn)
                    return class_qn if class_qn else class_name

        # Look for patterns like: const storage = Storage.getInstance()
        elif value_node.type == "call_expression":
            func_node = value_node.child_by_field_name("function")
            logger.debug(
                f"Call expression func_node type: {func_node.type if func_node else 'None'}"
            )

            # Handle method calls like Storage.getInstance()
            if func_node and func_node.type == "member_expression":
                # Extract the full method call text
                method_call_text = self._extract_js_method_call(func_node)
                logger.debug(f"Extracted method call: {method_call_text}")
                if method_call_text:
                    # Try to infer the return type of the method
                    inferred_type = self._infer_js_method_return_type(
                        method_call_text, module_qn
                    )
                    if inferred_type:
                        logger.debug(
                            f"JS type inference: {method_call_text}() returns {inferred_type}"
                        )
                        return inferred_type
                    else:
                        logger.debug(
                            f"Could not infer return type for {method_call_text}()"
                        )

            # Handle simple function calls like: const rect = Rectangle()
            elif func_node and func_node.type == "identifier":
                func_name = func_node.text
                if func_name:
                    # Assume factory functions return their own type
                    return str(func_name.decode("utf8"))

        logger.debug(
            f"No type inference pattern matched for value node type: {value_node.type}"
        )
        return None

    def _extract_js_method_call(self, member_expr_node: Node) -> str | None:
        """Extract method call text from JavaScript member expression like Storage.getInstance."""
        try:
            # member_expression has 'object' and 'property' fields
            object_node = member_expr_node.child_by_field_name("object")
            property_node = member_expr_node.child_by_field_name("property")

            if object_node and property_node:
                object_text = object_node.text
                property_text = property_node.text

                if object_text and property_text:
                    object_name = object_text.decode("utf8")
                    property_name = property_text.decode("utf8")
                    return f"{object_name}.{property_name}"
        except Exception as e:
            logger.debug(f"Error extracting JS method call: {e}")

        return None

    def _infer_js_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        """
        Infer the return type of a JavaScript method call.
        For example: Storage.getInstance() should return 'Storage'
        """
        try:
            # Split method call like "Storage.getInstance" into parts
            parts = method_call.split(".")
            if len(parts) != 2:
                logger.debug(f"Method call {method_call} doesn't have 2 parts")
                return None

            class_name, method_name = parts

            # Resolve the class name to its fully qualified name
            class_qn = self._resolve_js_class_name(class_name, module_qn)
            if not class_qn:
                logger.debug(
                    f"Could not resolve class name {class_name} in module {module_qn}"
                )
                return None

            logger.debug(f"Resolved {class_name} to {class_qn}")

            # Look up the method in the function registry
            method_qn = f"{class_qn}.{method_name}"
            logger.debug(f"Looking for method {method_qn} in function registry")

            # Find the method's AST node and analyze its return statements
            method_node = self._find_method_ast_node(method_qn)
            if not method_node:
                logger.debug(f"Could not find AST node for method {method_qn}")
                return None

            # Analyze the return statements to infer type
            return_type = self._analyze_js_return_statements(method_node, method_qn)
            logger.debug(
                f"Analyzed return statements for {method_qn}, got type: {return_type}"
            )
            return return_type

        except Exception as e:
            logger.debug(
                f"Error inferring JS method return type for {method_call}: {e}"
            )

        return None

    def _resolve_js_class_name(self, class_name: str, module_qn: str) -> str | None:
        """Resolve a JavaScript class name to its fully qualified name."""
        # First check if it's imported
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]

                # For JavaScript, the import might point to a module (e.g., js_test.storage.Storage)
                # but the actual class QN includes the class name again (e.g., js_test.storage.Storage.Storage)
                # Try appending the class name to see if that's a valid class
                full_class_qn = f"{imported_qn}.{class_name}"
                if (
                    full_class_qn in self.function_registry
                    and self.function_registry[full_class_qn] == "Class"
                ):
                    return full_class_qn

                # Otherwise return the imported QN as-is
                return imported_qn

        # Then check if it's in the same module
        local_class_qn = f"{module_qn}.{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == "Class"
        ):
            return local_class_qn

        return None

    def _analyze_js_return_statements(
        self, method_node: Node, method_qn: str
    ) -> str | None:
        """Analyze JavaScript return statements to infer return type."""
        # Find all return statements
        return_nodes: list[Node] = []
        self._find_js_return_statements(method_node, return_nodes)

        for return_node in return_nodes:
            # Get the returned expression (skip "return" keyword)
            for child in return_node.children:
                if child.type == "return":
                    continue

                # Analyze what's being returned
                inferred_type = self._analyze_js_return_expression(child, method_qn)
                if inferred_type:
                    return inferred_type

        return None

    def _find_js_return_statements(self, node: Node, return_nodes: list[Node]) -> None:
        """Find all return statements in a JavaScript function.

        Uses iterative stack-based traversal to prevent RecursionError
        for deeply nested code.
        """
        stack: list[Node] = [node]

        while stack:
            current = stack.pop()

            if current.type == "return_statement":
                return_nodes.append(current)

            # Process children in reverse order to maintain traversal order
            stack.extend(reversed(current.children))

    def _analyze_js_return_expression(
        self, expr_node: Node, method_qn: str
    ) -> str | None:
        """Analyze a JavaScript return expression to infer its type."""
        # Handle: return new Storage()
        if expr_node.type == "new_expression":
            constructor_node = expr_node.child_by_field_name("constructor")
            if constructor_node and constructor_node.type == "identifier":
                constructor_text = constructor_node.text
                if constructor_text:
                    class_name: str = constructor_text.decode("utf8")
                    # Return the full class QN from method QN
                    # For JS: "project.storage.Storage.Storage.getInstance" -> "project.storage.Storage.Storage"
                    qn_parts = method_qn.split(".")
                    if len(qn_parts) >= 2:
                        return ".".join(qn_parts[:-1])  # Everything except method name
                    return class_name

        # Handle: return this
        elif expr_node.type == "this":
            # Return the full class QN from method QN
            qn_parts = method_qn.split(".")
            if len(qn_parts) >= 2:
                return ".".join(qn_parts[:-1])  # Everything except method name

        # Handle: return Storage.instance or return this.instance
        elif expr_node.type == "member_expression":
            object_node = expr_node.child_by_field_name("object")
            if object_node:
                if object_node.type == "this":
                    # return this.instance -> return the class type
                    qn_parts = method_qn.split(".")
                    if len(qn_parts) >= 2:
                        return ".".join(qn_parts[:-1])
                elif object_node.type == "identifier":
                    object_text = object_node.text
                    if object_text:
                        object_name = object_text.decode("utf8")
                        # Handle: return Storage.instance in static method
                        # Assume it returns the class type
                        qn_parts = method_qn.split(".")
                        if len(qn_parts) >= 2 and object_name == qn_parts[-2]:
                            return ".".join(qn_parts[:-1])

        return None

    def find_js_method_in_ast(
        self, root_node: Node, class_name: str, method_name: str
    ) -> Node | None:
        """Find a specific method within a JavaScript/TypeScript class in the AST."""
        # Use stack-based traversal to find the class
        stack: list[Node] = [root_node]

        while stack:
            current = stack.pop()

            # Look for class declaration
            if current.type == "class_declaration":
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    found_class_name = name_node.text.decode("utf8")
                    if found_class_name == class_name:
                        # Found the class, now find the method
                        body_node = current.child_by_field_name("body")
                        if body_node:
                            return self._find_js_method_in_class_body(
                                body_node, method_name
                            )

            stack.extend(reversed(current.children))

        return None

    def _find_js_method_in_class_body(
        self, class_body_node: Node, method_name: str
    ) -> Node | None:
        """Find a method by name within a JavaScript class body."""
        for child in class_body_node.children:
            # Look for method_definition nodes
            if child.type == "method_definition":
                name_node = child.child_by_field_name("name")
                if name_node and name_node.text:
                    found_name = name_node.text.decode("utf8")
                    if found_name == method_name:
                        return child

        return None
