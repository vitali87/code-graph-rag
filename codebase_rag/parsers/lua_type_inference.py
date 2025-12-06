from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node

from .import_processor import ImportProcessor

if TYPE_CHECKING:
    pass


class LuaTypeInferenceEngine:
    """Handles precise type inference for Lua using tree-sitter AST analysis."""

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: Any,
        project_name: str,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name

    def build_lua_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        """Build local variable type map for Lua using stack-based traversal."""
        local_var_types: dict[str, str] = {}

        # Use stack-based traversal to find ALL variable declarations
        stack: list[Node] = [caller_node]

        while stack:
            current = stack.pop()

            # Look for variable declarations: local storage = Storage:getInstance()
            if current.type == "variable_declaration":
                # Find the assignment statement inside
                assignment = None
                for child in current.children:
                    if child.type == "assignment_statement":
                        assignment = child
                        break

                if assignment:
                    # Get variable names and values
                    var_names = []
                    var_values = []

                    for child in assignment.children:
                        if child.type == "variable_list":
                            for var_node in child.children:
                                if var_node.type == "identifier" and var_node.text:
                                    var_names.append(var_node.text.decode("utf8"))
                        elif child.type == "expression_list":
                            for expr_node in child.children:
                                if expr_node.type == "function_call":
                                    var_values.append(expr_node)

                    # Match up names with values (Lua allows multiple assignment)
                    for i, var_name in enumerate(var_names):
                        if i < len(var_values):
                            value_node = var_values[i]
                            var_type = self._infer_lua_variable_type_from_value(
                                value_node, module_qn
                            )
                            if var_type:
                                local_var_types[var_name] = var_type
                                logger.debug(
                                    f"Inferred Lua variable: {var_name} -> {var_type}"
                                )

            # Queue children for traversal (reversed to maintain order)
            stack.extend(reversed(current.children))

        logger.debug(
            f"Built Lua variable type map with {len(local_var_types)} variables"
        )
        return local_var_types

    def _infer_lua_variable_type_from_value(
        self, value_node: Node, module_qn: str
    ) -> str | None:
        """Infer the type of a Lua variable from its value expression."""
        # Look for method calls like Storage:getInstance()
        if value_node.type == "function_call":
            # Check if it's a method call (has method_index_expression)
            for child in value_node.children:
                if child.type == "method_index_expression":
                    # Extract Class:method pattern
                    class_name = None
                    method_name = None

                    for grandchild in child.children:
                        if grandchild.type == "identifier":
                            if class_name is None:
                                class_name = (
                                    grandchild.text.decode("utf8")
                                    if grandchild.text
                                    else None
                                )
                            else:
                                method_name = (
                                    grandchild.text.decode("utf8")
                                    if grandchild.text
                                    else None
                                )

                    if class_name and method_name:
                        # Try to resolve the class name
                        class_qn = self._resolve_lua_class_name(class_name, module_qn)
                        if class_qn:
                            # For now, assume static-like methods return the class type
                            # This works for singleton getInstance() patterns
                            logger.debug(
                                f"Lua type inference: {class_name}:{method_name}() returns {class_qn}"
                            )
                            return class_qn

        return None

    def _resolve_lua_class_name(self, class_name: str, module_qn: str) -> str | None:
        """Resolve a Lua table/class name to its fully qualified name."""
        # Check if it's imported
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]
                # For Lua, imports map to module QNs, append class name
                # e.g., lua_test.storage → lua_test.storage.Storage
                # Lua tables aren't always registered in function_registry, so always use full path
                full_class_qn = f"{imported_qn}.{class_name}"
                return full_class_qn

        # Check if it's in the same module
        local_class_qn = f"{module_qn}.{class_name}"
        if local_class_qn in self.function_registry:
            return local_class_qn

        # For Lua, classes might not be registered as entities, but their methods are
        # Check if any method of this class exists (e.g., Application:new)
        method_prefix = f"{local_class_qn}:"
        for qn in self.function_registry.keys():
            if qn.startswith(method_prefix):
                # Found a method, so the class exists
                return local_class_qn

        return None
