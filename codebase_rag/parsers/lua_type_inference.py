from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node

from .import_processor import ImportProcessor
from .utils import safe_decode_text

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

        stack: list[Node] = [caller_node]

        while stack:
            current = stack.pop()

            if current.type == "variable_declaration":
                assignment = None
                for child in current.children:
                    if child.type == "assignment_statement":
                        assignment = child
                        break

                if assignment:
                    var_names = []
                    var_values = []

                    for child in assignment.children:
                        if child.type == "variable_list":
                            for var_node in child.children:
                                if var_node.type == "identifier":
                                    if decoded_name := safe_decode_text(var_node):
                                        var_names.append(decoded_name)
                        elif child.type == "expression_list":
                            for expr_node in child.children:
                                if expr_node.type == "function_call":
                                    var_values.append(expr_node)

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

            stack.extend(reversed(current.children))

        logger.debug(
            f"Built Lua variable type map with {len(local_var_types)} variables"
        )
        return local_var_types

    def _infer_lua_variable_type_from_value(
        self, value_node: Node, module_qn: str
    ) -> str | None:
        """Infer the type of a Lua variable from its value expression."""
        if value_node.type == "function_call":
            for child in value_node.children:
                if child.type == "method_index_expression":
                    class_name = None
                    method_name = None

                    for grandchild in child.children:
                        if grandchild.type == "identifier":
                            if class_name is None:
                                class_name = safe_decode_text(grandchild)
                            else:
                                method_name = safe_decode_text(grandchild)

                    if class_name and method_name:
                        class_qn = self._resolve_lua_class_name(class_name, module_qn)
                        if class_qn:
                            logger.debug(
                                f"Lua type inference: {class_name}:{method_name}() returns {class_qn}"
                            )
                            return class_qn

        return None

    def _resolve_lua_class_name(self, class_name: str, module_qn: str) -> str | None:
        """Resolve a Lua table/class name to its fully qualified name."""
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]
                full_class_qn = f"{imported_qn}.{class_name}"
                return full_class_qn

        local_class_qn = f"{module_qn}.{class_name}"
        if local_class_qn in self.function_registry:
            return local_class_qn

        # For Lua, classes might not be registered as entities, but their methods are
        # Check if any method of this class exists (e.g., Application:new)
        # Use trie's find_with_prefix for O(k) lookup, then check for Lua method separator
        method_prefix = f"{local_class_qn}:"
        for qn, _ in self.function_registry.find_with_prefix(local_class_qn):
            if qn.startswith(method_prefix):
                return local_class_qn

        return None
