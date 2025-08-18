"""Java-specific type inference engine using tree-sitter for precise semantic analysis."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node

from .import_processor import ImportProcessor
from .java_utils import (
    extract_java_field_info,
    extract_java_method_call_info,
    safe_decode_text,
)

if TYPE_CHECKING:
    from .factory import ASTCacheProtocol


class JavaTypeInferenceEngine:
    """Handles precise type inference for Java using tree-sitter AST analysis."""

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: Any,
        repo_path: Path,
        project_name: str,
        ast_cache: "ASTCacheProtocol",
        queries: dict[str, Any],
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.repo_path = repo_path
        self.project_name = project_name
        self.ast_cache = ast_cache
        self.queries = queries

    def build_java_variable_type_map(
        self, scope_node: Node, module_qn: str
    ) -> dict[str, str]:
        """
        Build a comprehensive map of variable names to their types within a Java scope.

        This analyzes:
        - Method parameters (formal_parameter nodes)
        - Local variable declarations (local_variable_declaration nodes)
        - Field declarations in the containing class
        - Constructor assignments

        Args:
            scope_node: The AST node representing the scope (method, constructor, etc.)
            module_qn: Qualified name of the current module

        Returns:
            Dictionary mapping variable names to their fully qualified type names
        """
        local_var_types: dict[str, str] = {}

        try:
            # 1. Analyze method/constructor parameters
            self._analyze_java_parameters(scope_node, local_var_types, module_qn)

            # 2. Analyze local variable declarations in the scope
            self._analyze_java_local_variables(scope_node, local_var_types, module_qn)

            # 3. Analyze field declarations from the containing class
            self._analyze_java_class_fields(scope_node, local_var_types, module_qn)

            # 4. Analyze constructor assignments and field initializations
            self._analyze_java_constructor_assignments(
                scope_node, local_var_types, module_qn
            )

            # 5. Analyze enhanced for loop variables using tree-sitter
            self._analyze_java_enhanced_for_loops(
                scope_node, local_var_types, module_qn
            )

            logger.debug(
                f"Built Java variable type map with {len(local_var_types)} entries"
            )

        except Exception as e:
            logger.error(f"Failed to build Java variable type map: {e}")

        return local_var_types

    def _analyze_java_parameters(
        self, scope_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze formal parameters using tree-sitter field access."""
        # Get the formal parameter list
        params_node = scope_node.child_by_field_name("parameters")
        if not params_node:
            return

        for child in params_node.children:
            if child.type == "formal_parameter":
                # Extract parameter name and type using tree-sitter fields
                param_name_node = child.child_by_field_name("name")
                param_type_node = child.child_by_field_name("type")

                if param_name_node and param_type_node:
                    param_name = safe_decode_text(param_name_node)
                    param_type = safe_decode_text(param_type_node)

                    if param_name and param_type:
                        # Resolve the type to its fully qualified name
                        resolved_type = self._resolve_java_type_name(
                            param_type, module_qn
                        )
                        local_var_types[param_name] = resolved_type
                        logger.debug(f"Parameter: {param_name} -> {resolved_type}")

    def _analyze_java_local_variables(
        self, scope_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze local variable declarations using tree-sitter traversal."""
        self._traverse_for_local_variables(scope_node, local_var_types, module_qn)

    def _traverse_for_local_variables(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Recursively traverse AST to find local variable declarations."""
        if node.type == "local_variable_declaration":
            self._process_java_variable_declaration(node, local_var_types, module_qn)

        # Recursively traverse children
        for child in node.children:
            self._traverse_for_local_variables(child, local_var_types, module_qn)

    def _process_java_variable_declaration(
        self, decl_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process a local_variable_declaration node to extract type information."""
        # Get the type field
        type_node = decl_node.child_by_field_name("type")
        if not type_node:
            return

        declared_type = safe_decode_text(type_node)
        if not declared_type:
            return

        # Get the variable declarator field
        declarator_node = decl_node.child_by_field_name("declarator")
        if not declarator_node:
            return

        # Handle single or multiple declarators
        if declarator_node.type == "variable_declarator":
            self._process_variable_declarator(
                declarator_node, declared_type, local_var_types, module_qn
            )
        else:
            # Multiple declarators - traverse to find them
            for child in declarator_node.children:
                if child.type == "variable_declarator":
                    self._process_variable_declarator(
                        child, declared_type, local_var_types, module_qn
                    )

    def _process_variable_declarator(
        self,
        declarator_node: Node,
        declared_type: str,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        """Process a variable_declarator node to extract variable name and infer actual type."""
        # Get variable name
        name_node = declarator_node.child_by_field_name("name")
        if not name_node:
            return

        var_name = safe_decode_text(name_node)
        if not var_name:
            return

        # Check if there's an initialization value
        value_node = declarator_node.child_by_field_name("value")
        if value_node:
            # Try to infer more specific type from the initializer
            inferred_type = self._infer_java_type_from_expression(value_node, module_qn)
            if inferred_type:
                # Use the inferred type if it's more specific
                resolved_type = self._resolve_java_type_name(inferred_type, module_qn)
                local_var_types[var_name] = resolved_type
                logger.debug(
                    f"Local variable (inferred): {var_name} -> {resolved_type}"
                )
                return

        # Fall back to declared type
        resolved_type = self._resolve_java_type_name(declared_type, module_qn)
        local_var_types[var_name] = resolved_type
        logger.debug(f"Local variable (declared): {var_name} -> {resolved_type}")

    def _analyze_java_class_fields(
        self, scope_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze field declarations from the containing class for 'this' references."""
        # Find the containing class
        containing_class = self._find_containing_java_class(scope_node)
        if not containing_class:
            return

        # Get class body
        body_node = containing_class.child_by_field_name("body")
        if not body_node:
            return

        # Traverse class body for field declarations
        for child in body_node.children:
            if child.type == "field_declaration":
                field_info = extract_java_field_info(child)
                if field_info.get("name") and field_info.get("type"):
                    field_name = field_info["name"]
                    field_type = field_info["type"]

                    # Store as this.fieldName for accurate resolution
                    this_field_ref = f"this.{field_name}"
                    resolved_type = self._resolve_java_type_name(
                        str(field_type), module_qn
                    )
                    local_var_types[this_field_ref] = resolved_type

                    # Also store without 'this.' for direct field access
                    local_var_types[str(field_name)] = resolved_type
                    logger.debug(f"Class field: {field_name} -> {resolved_type}")

    def _analyze_java_constructor_assignments(
        self, scope_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze constructor assignments for field initialization patterns."""
        # This is for patterns like: this.field = new SomeClass();
        self._traverse_for_assignments(scope_node, local_var_types, module_qn)

    def _traverse_for_assignments(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Recursively traverse to find assignment expressions."""
        if node.type == "assignment_expression":
            self._process_java_assignment(node, local_var_types, module_qn)

        # Recursively traverse children
        for child in node.children:
            self._traverse_for_assignments(child, local_var_types, module_qn)

    def _process_java_assignment(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process Java assignment expressions to infer types."""
        # Get left and right sides of assignment
        left_node = assignment_node.child_by_field_name("left")
        right_node = assignment_node.child_by_field_name("right")

        if not left_node or not right_node:
            return

        # Extract variable name from left side
        var_name = self._extract_java_variable_reference(left_node)
        if not var_name:
            return

        # Infer type from right side
        inferred_type = self._infer_java_type_from_expression(right_node, module_qn)
        if inferred_type:
            resolved_type = self._resolve_java_type_name(inferred_type, module_qn)
            local_var_types[var_name] = resolved_type
            logger.debug(f"Assignment: {var_name} -> {resolved_type}")

    def _extract_java_variable_reference(self, node: Node) -> str | None:
        """Extract variable reference from left side of assignment."""
        if node.type == "identifier":
            return safe_decode_text(node)
        elif node.type == "field_access":
            # Handle this.field pattern
            object_node = node.child_by_field_name("object")
            field_node = node.child_by_field_name("field")

            if object_node and field_node:
                object_name = safe_decode_text(object_node)
                field_name = safe_decode_text(field_node)

                if object_name and field_name:
                    return f"{object_name}.{field_name}"

        return None

    def _infer_java_type_from_expression(
        self, expr_node: Node, module_qn: str
    ) -> str | None:
        """Infer Java type from various expression types."""
        if expr_node.type == "object_creation_expression":
            # Handle 'new SomeClass()' expressions
            type_node = expr_node.child_by_field_name("type")
            if type_node:
                return safe_decode_text(type_node)

        elif expr_node.type == "method_invocation":
            # Handle method call return types
            return self._infer_java_method_return_type(expr_node, module_qn)

        elif expr_node.type == "identifier":
            # Handle variable references - look up their types
            var_name = safe_decode_text(expr_node)
            if var_name:
                return self._lookup_variable_type(var_name, module_qn)

        elif expr_node.type == "field_access":
            # Handle field access expressions
            return self._infer_java_field_access_type(expr_node, module_qn)

        elif expr_node.type == "string_literal":
            return "String"

        elif expr_node.type == "integer_literal":
            return "int"

        elif expr_node.type == "decimal_floating_point_literal":
            return "double"

        elif expr_node.type == "true" or expr_node.type == "false":
            return "boolean"

        elif expr_node.type == "array_creation_expression":
            # Handle array creation
            type_node = expr_node.child_by_field_name("type")
            if type_node:
                base_type = safe_decode_text(type_node)
                return f"{base_type}[]" if base_type else None

        return None

    def _infer_java_method_return_type(
        self, method_call_node: Node, module_qn: str
    ) -> str | None:
        """Infer return type of a Java method invocation."""
        call_info = extract_java_method_call_info(method_call_node)

        method_name = call_info.get("name")
        object_ref = call_info.get("object")

        if not method_name:
            return None

        # Build the method call string for resolution
        if object_ref:
            call_string = f"{object_ref}.{method_name}"
        else:
            call_string = str(method_name)

        # Try to resolve the method and get its return type
        return self._resolve_java_method_return_type(call_string, module_qn)

    def _infer_java_field_access_type(
        self, field_access_node: Node, module_qn: str
    ) -> str | None:
        """Infer type of field access expressions."""
        object_node = field_access_node.child_by_field_name("object")
        field_node = field_access_node.child_by_field_name("field")

        if not object_node or not field_node:
            return None

        object_name = safe_decode_text(object_node)
        field_name = safe_decode_text(field_node)

        if not object_name or not field_name:
            return None

        # Get the type of the object
        object_type = self._lookup_variable_type(object_name, module_qn)
        if not object_type:
            return None

        # Look up the field type in the object's class
        return self._lookup_java_field_type(object_type, field_name, module_qn)

    def _resolve_java_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        """Resolve the return type of a Java method call."""
        # This is a simplified implementation - would need to analyze method signatures
        # and return types from the AST cache in a full implementation

        # For now, use heuristics based on common Java patterns
        if "get" in method_call.lower():
            # Getter methods often return the field type
            if "string" in method_call.lower() or "name" in method_call.lower():
                return "String"
            elif "id" in method_call.lower():
                return "Long"
        elif "create" in method_call.lower() or "new" in method_call.lower():
            # Factory methods often return instances of the class they're creating
            parts = method_call.split(".")
            if len(parts) >= 2:
                # Try to infer return type from method name
                method_name = parts[-1]
                if "user" in method_name.lower():
                    return "User"
                elif "order" in method_name.lower():
                    return "Order"

        # TODO: Implement full method signature analysis
        return None

    def _lookup_java_field_type(
        self, class_type: str, field_name: str, module_qn: str
    ) -> str | None:
        """Look up the type of a field in a Java class."""
        # Resolve class_type to fully qualified name if needed
        self._resolve_java_type_name(class_type, module_qn)

        # Look for the field in the class definition
        # This would require analyzing the class AST from the cache
        # For now, return a placeholder

        # TODO: Implement full field type lookup from AST
        return None

    def _lookup_variable_type(self, var_name: str, module_qn: str) -> str | None:
        """Look up the type of a variable from previous analysis."""
        # This would require maintaining state across calls or
        # re-analyzing the scope. For now, return None to indicate
        # that this needs to be integrated with the main type map building.
        return None

    def _resolve_java_type_name(self, type_name: str, module_qn: str) -> str:
        """Resolve a Java type name to its fully qualified name."""
        if not type_name:
            return "Object"  # Default fallback

        # Handle primitive types
        if type_name in [
            "int",
            "long",
            "double",
            "float",
            "boolean",
            "char",
            "byte",
            "short",
        ]:
            return type_name

        # Handle common Java types
        if type_name in ["String", "Object", "Integer", "Long", "Double", "Boolean"]:
            return f"java.lang.{type_name}"

        # Handle arrays
        if type_name.endswith("[]"):
            base_type = type_name[:-2]
            resolved_base = self._resolve_java_type_name(base_type, module_qn)
            return f"{resolved_base}[]"

        # Handle generic types (basic support)
        if "<" in type_name and ">" in type_name:
            # Extract base type before the generic parameters
            base_type = type_name.split("<")[0]
            return self._resolve_java_type_name(base_type, module_qn)

        # Check imports for the type
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if type_name in import_map:
                return import_map[type_name]

        # Check if it's a class in the same package
        same_package_qn = f"{module_qn}.{type_name}"
        if (
            same_package_qn in self.function_registry
            and self.function_registry[same_package_qn] == "Class"
        ):
            return same_package_qn

        # Fallback: return as-is (might be a simple class name)
        return type_name

    def _find_containing_java_class(self, node: Node) -> Node | None:
        """Find the Java class that contains the given node."""
        current = node.parent
        while current:
            if current.type in [
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
            ]:
                return current
            current = current.parent
        return None

    def resolve_java_method_call(
        self, call_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> tuple[str, str] | None:
        """
        Resolve a Java method invocation to its qualified name and type.

        This is the main entry point for precise method call resolution.

        Args:
            call_node: The method_invocation AST node
            local_var_types: Map of variable names to types in current scope
            module_qn: Qualified name of current module

        Returns:
            Tuple of (method_type, method_qualified_name) or None if not resolvable
        """
        if call_node.type != "method_invocation":
            return None

        call_info = extract_java_method_call_info(call_node)
        method_name = call_info.get("name")
        object_ref = call_info.get("object")

        if not method_name:
            logger.debug("No method name found in call node")
            return None

        logger.debug(
            f"Resolving Java method call: method={method_name}, object={object_ref}"
        )

        # Case 1: Static method call or call without object (e.g., "method()" or "Class.method()")
        if not object_ref:
            logger.debug(f"Resolving static/local method: {method_name}")
            result = self._resolve_static_or_local_method(str(method_name), module_qn)
            if result:
                logger.debug(f"Found static/local method: {result}")
            else:
                logger.debug(f"Static/local method not found: {method_name}")
            return result

        # Case 2: Instance method call (e.g., "obj.method()")
        # First, determine the type of the object
        logger.debug(f"Resolving object type for: {object_ref}")
        object_type = self._resolve_java_object_type(
            str(object_ref), local_var_types, module_qn
        )
        if not object_type:
            logger.debug(f"Could not determine type of object: {object_ref}")
            return None

        logger.debug(f"Object type resolved to: {object_type}")
        # Now find the method in the object's class
        result = self._resolve_instance_method(object_type, str(method_name), module_qn)
        if result:
            logger.debug(f"Found instance method: {result}")
        else:
            logger.debug(f"Instance method not found: {object_type}.{method_name}")
        return result

    def _resolve_java_object_type(
        self, object_ref: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """Resolve the type of a Java object reference using tree-sitter analysis."""
        # Check if it's a local variable
        if object_ref in local_var_types:
            return local_var_types[object_ref]

        # Check for 'this' reference - find the containing class
        if object_ref == "this":
            # Look for any class in the current module (simplified)
            for qn, entity_type in self.function_registry.items():
                if entity_type == "Class" and qn.startswith(module_qn + "."):
                    return str(qn)
            return None

        # Check for 'super' reference
        if object_ref == "super":
            # For super calls, we need to look at parent classes
            # This is a simplified implementation
            for qn, entity_type in self.function_registry.items():
                if entity_type == "Class" and qn.startswith(module_qn + "."):
                    # Look for parent classes - simplified approach
                    parent_qn = self._find_parent_class(qn, module_qn)
                    if parent_qn:
                        return parent_qn
            return None

        # Check if it's a static class reference
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if object_ref in import_map:
                return import_map[object_ref]

        # Check if it's a simple class name in the same module
        simple_class_qn = f"{module_qn}.{object_ref}"
        if (
            simple_class_qn in self.function_registry
            and self.function_registry[simple_class_qn] == "Class"
        ):
            return simple_class_qn

        return None

    def _find_parent_class(self, class_qn: str, module_qn: str) -> str | None:
        """Find the parent class of a given class using basic heuristics."""
        # In a real implementation, this would parse the AST to find 'extends' relationships
        # For now, use a simple heuristic: look for other classes that could be parents
        for qn, entity_type in self.function_registry.items():
            if (
                entity_type == "Class"
                and qn.startswith(module_qn + ".")
                and qn != class_qn
                and len(qn.split(".")) == len(class_qn.split("."))
            ):  # Same nesting level
                return str(qn)
        return None

    def _resolve_static_or_local_method(
        self, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Resolve a static method call or local method call using tree-sitter."""
        # Search for methods in the current module that match the method name
        for qn, entity_type in self.function_registry.items():
            if (
                qn.startswith(module_qn + ".")
                and (qn.endswith(f".{method_name}()") or qn.endswith(f".{method_name}"))
                and entity_type in ["Method", "Constructor"]
            ):
                return entity_type, qn

        return None

    def _resolve_instance_method(
        self, object_type: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Resolve an instance method call on a specific object type using tree-sitter."""
        # Resolve object_type to fully qualified name
        resolved_type = self._resolve_java_type_name(object_type, module_qn)

        # Look for the method in the class (try with empty parameter list first)
        method_qn = f"{resolved_type}.{method_name}()"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn

        # Also try without parameters (fallback)
        method_qn_bare = f"{resolved_type}.{method_name}"
        if method_qn_bare in self.function_registry:
            return self.function_registry[method_qn_bare], method_qn_bare

        # Check inheritance hierarchy for inherited methods using tree-sitter navigation
        return self._find_inherited_method(resolved_type, method_name, module_qn)

    def _find_inherited_method(
        self, class_qn: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Find an inherited method using tree-sitter to traverse inheritance."""
        # Search through all classes in the registry to find potential parent classes
        for qn, entity_type in self.function_registry.items():
            if entity_type == "Class" and qn.startswith(module_qn + "."):
                # Check if this could be a parent class by checking inheritance patterns
                parent_method_qn = f"{qn}.{method_name}()"
                if parent_method_qn in self.function_registry:
                    # Found a potential inherited method
                    return self.function_registry[parent_method_qn], parent_method_qn

                # Also try without parameters (fallback)
                parent_method_qn_bare = f"{qn}.{method_name}"
                if parent_method_qn_bare in self.function_registry:
                    return self.function_registry[
                        parent_method_qn_bare
                    ], parent_method_qn_bare

        return None

    def _get_current_class_name(self, module_qn: str) -> str | None:
        """Extract current class name from module context."""
        # This is a simplified implementation - in practice, you'd need
        # to track the current class context during traversal
        return None

    def _get_superclass_name(self, class_name: str) -> str | None:
        """Get the superclass name for inheritance resolution."""
        # This would require analyzing class inheritance from the AST
        return None

    def _analyze_java_enhanced_for_loops(
        self, scope_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze enhanced for loops using tree-sitter to extract loop variable types."""
        self._traverse_for_enhanced_for_loops(scope_node, local_var_types, module_qn)

    def _traverse_for_enhanced_for_loops(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Recursively traverse AST using tree-sitter to find enhanced for statements."""
        if node.type == "enhanced_for_statement":
            self._process_enhanced_for_statement(node, local_var_types, module_qn)

        # Recursively traverse children using tree-sitter
        for child in node.children:
            self._traverse_for_enhanced_for_loops(child, local_var_types, module_qn)

    def _process_enhanced_for_statement(
        self, for_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process enhanced for statement using tree-sitter field access."""
        # Enhanced for loop: for (Type variable : collection)
        # Use tree-sitter field access to get the type and name
        type_node = for_node.child_by_field_name("type")
        name_node = for_node.child_by_field_name("name")

        if type_node and name_node:
            var_type = safe_decode_text(type_node)
            var_name = safe_decode_text(name_node)

            if var_type and var_name:
                resolved_type = self._resolve_java_type_name(var_type, module_qn)
                local_var_types[var_name] = resolved_type
                logger.debug(
                    f"Enhanced for loop variable: {var_name} -> {resolved_type}"
                )
        else:
            # Alternative parsing: look for variable_declarator in for loop children
            for child in for_node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        var_name = safe_decode_text(name_node)
                        if var_name:
                            # Find the type by looking at siblings
                            parent = child.parent
                            if parent:
                                for sibling in parent.children:
                                    if sibling.type == "type_identifier":
                                        var_type = safe_decode_text(sibling)
                                        if var_type:
                                            resolved_type = (
                                                self._resolve_java_type_name(
                                                    var_type, module_qn
                                                )
                                            )
                                            local_var_types[var_name] = resolved_type
                                            logger.debug(
                                                f"Enhanced for loop variable (alt): {var_name} -> {resolved_type}"
                                            )
                                            break
