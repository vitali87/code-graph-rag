from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node

from ..constants import SEPARATOR_DOT
from ..types_defs import NodeType, SimpleNameLookup
from .import_processor import ImportProcessor
from .java_utils import (
    extract_java_class_info,
    extract_java_field_info,
    extract_java_method_call_info,
    find_java_package_start_index,
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

        self._lookup_cache: dict[str, str | None] = {}
        self._lookup_in_progress: set[str] = set()

        self._fqn_to_module_qn: dict[str, list[str]] = self._build_fqn_lookup_map()

    def _build_fqn_lookup_map(self) -> dict[str, list[str]]:
        """
        Build a reverse lookup map from Java FQN to internal module QN.

        This pre-processing step converts the nested loop O(modules × functions)
        lookup into a O(1) dictionary lookup.

        Example:
            "com.example.utils.Helper" -> "project.src.main.java.com.example.utils.Helper"
        """
        fqn_map: dict[str, list[str]] = {}

        def _add_mapping(key: str, value: str) -> None:
            """Store all module candidates for a given FQN suffix."""
            modules = fqn_map.setdefault(key, [])
            if value not in modules:
                modules.append(value)

        for module_qn in self.module_qn_to_file_path.keys():
            parts = module_qn.split(SEPARATOR_DOT)
            package_start_idx = find_java_package_start_index(parts)

            if package_start_idx:
                simple_class_name = SEPARATOR_DOT.join(parts[package_start_idx:])
                if simple_class_name:
                    _add_mapping(simple_class_name, module_qn)

                    class_parts = simple_class_name.split(SEPARATOR_DOT)
                    for j in range(1, len(class_parts)):
                        suffix = SEPARATOR_DOT.join(class_parts[j:])
                        _add_mapping(suffix, module_qn)

        return fqn_map

    def _module_qn_to_java_fqn(self, module_qn: str) -> str | None:
        """Convert an internal module QN to a Java fully qualified class name."""
        parts = module_qn.split(SEPARATOR_DOT)
        package_start_idx = find_java_package_start_index(parts)
        if package_start_idx is None:
            return None
        class_parts = parts[package_start_idx:]
        return SEPARATOR_DOT.join(class_parts) if class_parts else None

    def _calculate_module_distance(
        self, candidate_qn: str, caller_module_qn: str
    ) -> int:
        """Heuristic distance between the caller and a candidate module."""
        caller_parts = caller_module_qn.split(SEPARATOR_DOT)
        candidate_parts = candidate_qn.split(SEPARATOR_DOT)

        common_prefix = 0
        for caller_part, candidate_part in zip(caller_parts, candidate_parts):
            if caller_part == candidate_part:
                common_prefix += 1
            else:
                break

        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

        if (
            len(caller_parts) > 1
            and candidate_parts[: len(caller_parts) - 1] == caller_parts[:-1]
        ):
            base_distance -= 1

        return max(base_distance, 0)

    def _rank_module_candidates(
        self,
        candidates: list[str],
        class_qn: str,
        current_module_qn: str | None,
    ) -> list[str]:
        """Order candidate modules by how well they match the desired class."""
        if not candidates:
            return candidates

        if not current_module_qn:
            return candidates

        ranked: list[tuple[tuple[int, int, int], str]] = []
        for idx, candidate in enumerate(candidates):
            candidate_fqn = self._module_qn_to_java_fqn(candidate)

            if candidate_fqn == class_qn:
                match_penalty = 0
            elif candidate_fqn and class_qn.endswith(candidate_fqn):
                match_penalty = 1
            else:
                match_penalty = 2

            distance = self._calculate_module_distance(candidate, current_module_qn)
            ranked.append(((match_penalty, distance, idx), candidate))

        ranked.sort(key=lambda item: item[0])
        return [candidate for _, candidate in ranked]

    def _find_registry_entries_under(self, prefix: str) -> Iterable[tuple[str, str]]:
        """Yield registry entries beneath the given qualified-name prefix."""

        finder = getattr(self.function_registry, "find_with_prefix", None)
        if callable(finder):
            matches = list(finder(prefix))  # type: ignore[arg-type]
            if matches:
                return matches

        items = getattr(self.function_registry, "items", None)
        if callable(items):
            prefix_with_dot = f"{prefix}."
            return [
                (qn, method_type)
                for qn, method_type in items()
                if qn.startswith(prefix_with_dot) or qn == prefix
            ]

        return []

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
            self._analyze_java_parameters(scope_node, local_var_types, module_qn)

            self._analyze_java_local_variables(scope_node, local_var_types, module_qn)

            self._analyze_java_class_fields(scope_node, local_var_types, module_qn)

            self._analyze_java_constructor_assignments(
                scope_node, local_var_types, module_qn
            )

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
        params_node = scope_node.child_by_field_name("parameters")
        if not params_node:
            return

        for child in params_node.children:
            if child.type == "formal_parameter":
                param_name_node = child.child_by_field_name("name")
                param_type_node = child.child_by_field_name("type")

                if param_name_node and param_type_node:
                    param_name = safe_decode_text(param_name_node)
                    param_type = safe_decode_text(param_type_node)

                    if param_name and param_type:
                        resolved_type = self._resolve_java_type_name(
                            param_type, module_qn
                        )
                        local_var_types[param_name] = resolved_type
                        logger.debug(f"Parameter: {param_name} -> {resolved_type}")

            elif child.type == "spread_parameter":
                param_name = None
                param_type = None

                for subchild in child.children:
                    if subchild.type == "type_identifier":
                        decoded_text = safe_decode_text(subchild)
                        if decoded_text:
                            param_type = decoded_text + "[]"
                    elif subchild.type == "variable_declarator":
                        name_node = subchild.child_by_field_name("name")
                        if name_node:
                            param_name = safe_decode_text(name_node)

                if param_name and param_type:
                    resolved_type = self._resolve_java_type_name(param_type, module_qn)
                    local_var_types[param_name] = resolved_type
                    logger.debug(f"Varargs parameter: {param_name} -> {resolved_type}")

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

        for child in node.children:
            self._traverse_for_local_variables(child, local_var_types, module_qn)

    def _process_java_variable_declaration(
        self, decl_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process a local_variable_declaration node to extract type information."""
        type_node = decl_node.child_by_field_name("type")
        if not type_node:
            return

        declared_type = safe_decode_text(type_node)
        if not declared_type:
            return

        declarator_node = decl_node.child_by_field_name("declarator")
        if not declarator_node:
            return

        if declarator_node.type == "variable_declarator":
            self._process_variable_declarator(
                declarator_node, declared_type, local_var_types, module_qn
            )
        else:
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
        name_node = declarator_node.child_by_field_name("name")
        if not name_node:
            return

        var_name = safe_decode_text(name_node)
        if not var_name:
            return

        value_node = declarator_node.child_by_field_name("value")
        if value_node:
            inferred_type = self._infer_java_type_from_expression(value_node, module_qn)
            if inferred_type:
                resolved_type = self._resolve_java_type_name(inferred_type, module_qn)
                local_var_types[var_name] = resolved_type
                logger.debug(
                    f"Local variable (inferred): {var_name} -> {resolved_type}"
                )
                return

        resolved_type = self._resolve_java_type_name(declared_type, module_qn)
        local_var_types[var_name] = resolved_type
        logger.debug(f"Local variable (declared): {var_name} -> {resolved_type}")

    def _analyze_java_class_fields(
        self, scope_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze field declarations from the containing class for 'this' references."""
        containing_class = self._find_containing_java_class(scope_node)
        if not containing_class:
            return

        body_node = containing_class.child_by_field_name("body")
        if not body_node:
            return

        for child in body_node.children:
            if child.type == "field_declaration":
                field_info = extract_java_field_info(child)
                if field_info.get("name") and field_info.get("type"):
                    field_name = field_info["name"]
                    field_type = field_info["type"]

                    this_field_ref = f"this.{field_name}"
                    resolved_type = self._resolve_java_type_name(
                        str(field_type), module_qn
                    )
                    local_var_types[this_field_ref] = resolved_type

                    if str(field_name) not in local_var_types:
                        local_var_types[str(field_name)] = resolved_type
                    logger.debug(f"Class field: {field_name} -> {resolved_type}")

    def _analyze_java_constructor_assignments(
        self, scope_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Analyze constructor assignments for field initialization patterns."""
        self._traverse_for_assignments(scope_node, local_var_types, module_qn)

    def _traverse_for_assignments(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Recursively traverse to find assignment expressions."""
        if node.type == "assignment_expression":
            self._process_java_assignment(node, local_var_types, module_qn)

        for child in node.children:
            self._traverse_for_assignments(child, local_var_types, module_qn)

    def _process_java_assignment(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process Java assignment expressions to infer types."""
        left_node = assignment_node.child_by_field_name("left")
        right_node = assignment_node.child_by_field_name("right")

        if not left_node or not right_node:
            return

        var_name = self._extract_java_variable_reference(left_node)
        if not var_name:
            return

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
            type_node = expr_node.child_by_field_name("type")
            if type_node:
                return safe_decode_text(type_node)

        elif expr_node.type == "method_invocation":
            return self._infer_java_method_return_type(expr_node, module_qn)

        elif expr_node.type == "identifier":
            var_name = safe_decode_text(expr_node)
            if var_name:
                return self._lookup_variable_type(var_name, module_qn)

        elif expr_node.type == "field_access":
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

        if object_ref:
            call_string = f"{object_ref}.{method_name}"
        else:
            call_string = str(method_name)

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

        object_type = self._lookup_variable_type(object_name, module_qn)
        if not object_type:
            return None

        return self._lookup_java_field_type(object_type, field_name, module_qn)

    def _resolve_java_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        """Resolve the return type of a Java method call using AST analysis."""
        if not method_call:
            return None

        parts = method_call.split(SEPARATOR_DOT)
        if len(parts) < 2:
            method_name = method_call
            current_class_qn = self._get_current_class_name(module_qn)
            if current_class_qn:
                return self._find_method_return_type(current_class_qn, method_name)
        else:
            object_part = SEPARATOR_DOT.join(parts[:-1])
            method_name = parts[-1]

            if object_part in self.function_registry:
                return self._find_method_return_type(object_part, method_name)

            object_type = self._lookup_variable_type(object_part, module_qn)
            if object_type:
                return self._find_method_return_type(object_type, method_name)

            potential_class_qn = f"{module_qn}.{object_part}"
            if potential_class_qn in self.function_registry:
                return self._find_method_return_type(potential_class_qn, method_name)

        return self._heuristic_method_return_type(method_call)

    def _find_method_return_type(self, class_qn: str, method_name: str) -> str | None:
        """Find the return type of a method in a specific class using AST analysis."""
        if not class_qn or not method_name:
            return None

        parts = class_qn.split(SEPARATOR_DOT)
        if len(parts) < 2:
            return None

        module_qn = SEPARATOR_DOT.join(parts[:-1])
        target_class_name = parts[-1]

        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is None or file_path not in self.ast_cache:
            return None

        root_node, _ = self.ast_cache[file_path]

        return self._find_method_return_type_in_ast(
            root_node, target_class_name, method_name, module_qn
        )

    def _find_method_return_type_in_ast(
        self, node: Node, class_name: str, method_name: str, module_qn: str
    ) -> str | None:
        """Find method return type by traversing the AST."""
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node and safe_decode_text(name_node) == class_name:
                body_node = node.child_by_field_name("body")
                if body_node:
                    return self._search_methods_in_class_body(
                        body_node, method_name, module_qn
                    )

        for child in node.children:
            result = self._find_method_return_type_in_ast(
                child, class_name, method_name, module_qn
            )
            if result:
                return result

        return None

    def _search_methods_in_class_body(
        self, body_node: Node, method_name: str, module_qn: str
    ) -> str | None:
        """Search for a specific method in a class body and return its return type."""
        for child in body_node.children:
            if child.type == "method_declaration":
                name_node = child.child_by_field_name("name")
                if name_node and safe_decode_text(name_node) == method_name:
                    type_node = child.child_by_field_name("type")
                    if type_node:
                        return_type = safe_decode_text(type_node)
                        if return_type:
                            return self._resolve_java_type_name(return_type, module_qn)
        return None

    def _heuristic_method_return_type(self, method_call: str) -> str | None:
        """Fallback heuristics for common Java patterns when AST analysis fails."""
        if "get" in method_call.lower():
            if "string" in method_call.lower() or "name" in method_call.lower():
                return "java.lang.String"
            elif "id" in method_call.lower():
                return "java.lang.Long"
            elif "size" in method_call.lower() or "length" in method_call.lower():
                return "int"
        elif "create" in method_call.lower() or "new" in method_call.lower():
            parts = method_call.split(SEPARATOR_DOT)
            if len(parts) >= 2:
                method_name = parts[-1]
                if "user" in method_name.lower():
                    return "User"
                elif "order" in method_name.lower():
                    return "Order"
        elif "is" in method_call.lower() or "has" in method_call.lower():
            return "boolean"

        return None

    def _lookup_java_field_type(
        self, class_type: str, field_name: str, module_qn: str
    ) -> str | None:
        """Look up the type of a field in a Java class."""
        if not class_type or not field_name:
            return None

        resolved_class_type = self._resolve_java_type_name(class_type, module_qn)

        if SEPARATOR_DOT in resolved_class_type:
            class_qn = resolved_class_type
        else:
            class_qn = f"{module_qn}.{resolved_class_type}"

        parts = class_qn.split(SEPARATOR_DOT)
        if len(parts) < 2:
            return None

        target_module_qn = SEPARATOR_DOT.join(parts[:-1])
        target_class_name = parts[-1]

        file_path = self.module_qn_to_file_path.get(target_module_qn)
        if file_path is None or file_path not in self.ast_cache:
            return None

        root_node, _ = self.ast_cache[file_path]

        field_type = self._find_field_type_in_class(
            root_node, target_class_name, field_name, target_module_qn
        )

        return field_type

    def _lookup_variable_type(self, var_name: str, module_qn: str) -> str | None:
        """Look up the type of a variable by analyzing the module scope."""
        if not var_name or not module_qn:
            return None

        cache_key = f"{module_qn}:{var_name}"
        if cache_key in self._lookup_cache:
            return self._lookup_cache[cache_key]

        if cache_key in self._lookup_in_progress:
            return None

        self._lookup_in_progress.add(cache_key)

        try:
            module_parts = module_qn.split(SEPARATOR_DOT)
            if len(module_parts) < 2:
                result = None
            else:
                file_path = self.module_qn_to_file_path.get(module_qn)
                if file_path is None or file_path not in self.ast_cache:
                    result = None
                else:
                    root_node, _ = self.ast_cache[file_path]

                    variable_types = self.build_java_variable_type_map(
                        root_node, module_qn
                    )

                    if var_name in variable_types:
                        result = variable_types[var_name]
                    elif f"this.{var_name}" in variable_types:
                        result = variable_types[f"this.{var_name}"]
                    else:
                        result = None

            self._lookup_cache[cache_key] = result
            return result

        finally:
            self._lookup_in_progress.discard(cache_key)

    def _resolve_java_type_name(self, type_name: str, module_qn: str) -> str:
        """Resolve a Java type name to its fully qualified name."""
        if not type_name:
            return "Object"

        if SEPARATOR_DOT in type_name:
            return type_name

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

        if type_name in ["String", "Object", "Integer", "Long", "Double", "Boolean"]:
            return f"java.lang.{type_name}"

        if type_name.endswith("[]"):
            base_type = type_name[:-2]
            resolved_base = self._resolve_java_type_name(base_type, module_qn)
            return f"{resolved_base}[]"

        if "<" in type_name and ">" in type_name:
            base_type = type_name.split("<")[0]
            return self._resolve_java_type_name(base_type, module_qn)

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if type_name in import_map:
                return import_map[type_name]

        same_package_qn = f"{module_qn}.{type_name}"
        if same_package_qn in self.function_registry and self.function_registry[
            same_package_qn
        ] in ["Class", "Interface"]:
            return same_package_qn

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

        if not object_ref:
            logger.debug(f"Resolving static/local method: {method_name}")
            result = self._resolve_static_or_local_method(str(method_name), module_qn)
            if result:
                logger.debug(f"Found static/local method: {result}")
            else:
                logger.debug(f"Static/local method not found: {method_name}")
            return result

        logger.debug(f"Resolving object type for: {object_ref}")
        object_type = self._resolve_java_object_type(
            str(object_ref), local_var_types, module_qn
        )
        if not object_type:
            logger.debug(f"Could not determine type of object: {object_ref}")
            return None

        logger.debug(f"Object type resolved to: {object_type}")
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
        if object_ref in local_var_types:
            return local_var_types[object_ref]

        """(H) Check for 'this' reference - find the containing class (using trie for O(k) lookup)"""
        if object_ref == "this":
            for qn, entity_type in self.function_registry.find_with_prefix(module_qn):
                if entity_type == NodeType.CLASS:
                    return str(qn)
            return None

        """(H) Check for 'super' reference - for super calls, look at parent classes (using trie for O(k) lookup)"""
        if object_ref == "super":
            for qn, entity_type in self.function_registry.find_with_prefix(module_qn):
                if entity_type == NodeType.CLASS:
                    parent_qn = self._find_parent_class(qn)
                    if parent_qn:
                        return parent_qn
            return None

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if object_ref in import_map:
                return import_map[object_ref]

        simple_class_qn = f"{module_qn}.{object_ref}"
        if (
            simple_class_qn in self.function_registry
            and self.function_registry[simple_class_qn] == NodeType.CLASS
        ):
            return simple_class_qn

        return None

    def _find_parent_class(self, class_qn: str) -> str | None:
        """Find the parent class of a given class using actual inheritance data."""
        parent_classes = self.class_inheritance.get(class_qn, [])

        if parent_classes:
            return parent_classes[0]

        return None

    def _resolve_static_or_local_method(
        self, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Resolve a static method call or local method call using tree-sitter."""
        for qn, entity_type in self.function_registry.find_with_prefix(module_qn):
            if entity_type in ["Method", "Constructor"] and qn.split("(")[0].endswith(
                f".{method_name}"
            ):
                return entity_type, qn

        return None

    def _resolve_instance_method(
        self, object_type: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Resolve an instance method call on a specific object type using tree-sitter."""
        resolved_type = self._resolve_java_type_name(object_type, module_qn)

        method_result = self._find_method_with_any_signature(
            resolved_type, method_name, module_qn
        )
        if method_result:
            return method_result

        inherited_result = self._find_inherited_method(
            resolved_type, method_name, module_qn
        )
        if inherited_result:
            return inherited_result

        return self._find_interface_method(resolved_type, method_name, module_qn)

    def _find_method_with_any_signature(
        self, class_qn: str, method_name: str, current_module_qn: str | None = None
    ) -> tuple[str, str] | None:
        """Find a method with any parameter signature using function registry."""
        if class_qn:
            for qn, method_type in self._find_registry_entries_under(class_qn):
                if qn == class_qn:
                    continue
                suffix = qn[len(class_qn) :]
                if not suffix.startswith("."):
                    continue
                member = suffix[1:]
                if (
                    member == method_name
                    or member.startswith(f"{method_name}(")
                    or member == f"{method_name}()"
                ):
                    return method_type, qn

        if class_qn and not class_qn.startswith(self.project_name):
            suffixes = class_qn.split(SEPARATOR_DOT) if class_qn else []
            lookup_keys = [
                SEPARATOR_DOT.join(suffixes[i:]) for i in range(len(suffixes))
            ]
            if not lookup_keys:
                lookup_keys = [class_qn]

            candidate_modules: list[str] = []
            seen_modules: set[str] = set()

            for key in lookup_keys:
                if key in self._fqn_to_module_qn:
                    for module_candidate in self._fqn_to_module_qn[key]:
                        if module_candidate not in seen_modules:
                            candidate_modules.append(module_candidate)
                            seen_modules.add(module_candidate)

            ranked_candidates = self._rank_module_candidates(
                candidate_modules, class_qn, current_module_qn
            )

            simple_class_name = class_qn.split(SEPARATOR_DOT)[-1]

            for module_qn in ranked_candidates:
                registry_class_qn = f"{module_qn}.{simple_class_name}"
                for qn, method_type in self._find_registry_entries_under(
                    registry_class_qn
                ):
                    if qn == registry_class_qn:
                        continue
                    suffix = qn[len(registry_class_qn) :]
                    if not suffix.startswith("."):
                        continue
                    member = suffix[1:]
                    if (
                        member == method_name
                        or member.startswith(f"{method_name}(")
                        or member == f"{method_name}()"
                    ):
                        return method_type, qn

        return None

    def _find_inherited_method(
        self, class_qn: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Find an inherited method using precise tree-sitter inheritance traversal."""
        superclass_qn = self._get_superclass_name(class_qn)
        if not superclass_qn:
            return None

        method_result = self._find_method_with_any_signature(
            superclass_qn, method_name, module_qn
        )
        if method_result:
            return method_result

        return self._find_inherited_method(superclass_qn, method_name, module_qn)

    def _find_interface_method(
        self, class_qn: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Find a method in implemented interfaces using precise tree-sitter analysis."""
        implemented_interfaces = self._get_implemented_interfaces(class_qn)

        for interface_qn in implemented_interfaces:
            method_result = self._find_method_with_any_signature(
                interface_qn, method_name, module_qn
            )
            if method_result:
                return method_result

        return None

    def _get_implemented_interfaces(self, class_qn: str) -> list[str]:
        """Get all interfaces implemented by a class using tree-sitter AST analysis."""
        parts = class_qn.split(SEPARATOR_DOT)
        if len(parts) < 2:
            return []

        module_qn = SEPARATOR_DOT.join(parts[:-1])
        target_class_name = parts[-1]

        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is None or file_path not in self.ast_cache:
            return []

        root_node, _ = self.ast_cache[file_path]

        interfaces = self._find_interfaces_using_ast(
            root_node, target_class_name, module_qn
        )
        return interfaces

    def _find_interfaces_using_ast(
        self, node: Node, target_class_name: str, module_qn: str
    ) -> list[str]:
        """Find implemented interfaces using precise tree-sitter AST traversal."""
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node and safe_decode_text(name_node) == target_class_name:
                interfaces_node = node.child_by_field_name("interfaces")
                if interfaces_node:
                    interface_list: list[str] = []
                    self._extract_interface_names(
                        interfaces_node, interface_list, module_qn
                    )
                    return interface_list

        for child in node.children:
            result = self._find_interfaces_using_ast(
                child, target_class_name, module_qn
            )
            if result:
                return result

        return []

    def _extract_interface_names(
        self, interfaces_node: Node, interface_list: list[str], module_qn: str
    ) -> None:
        """Extract interface names from the interfaces list using tree-sitter."""
        for child in interfaces_node.children:
            if child.type == "type_identifier":
                interface_name = safe_decode_text(child)
                if interface_name:
                    resolved_interface = self._resolve_java_type_name(
                        interface_name, module_qn
                    )
                    interface_list.append(resolved_interface)
            elif child.children:
                self._extract_interface_names(child, interface_list, module_qn)

    def _get_current_class_name(self, module_qn: str) -> str | None:
        """Extract current class name from AST context using precise tree-sitter traversal."""
        module_parts = module_qn.split(SEPARATOR_DOT)
        if len(module_parts) < 2:
            return None

        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is None or file_path not in self.ast_cache:
            return None

        root_node, _ = self.ast_cache[file_path]

        class_names: list[str] = []
        self._traverse_for_class_declarations(root_node, class_names)

        if class_names:
            return f"{module_qn}.{class_names[0]}"

        return None

    def _traverse_for_class_declarations(
        self, node: Node, class_names: list[str]
    ) -> None:
        """Recursively traverse AST using tree-sitter to find class declarations."""
        if node.type in [
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
        ]:
            name_node = node.child_by_field_name("name")
            if name_node:
                class_name = safe_decode_text(name_node)
                if class_name:
                    class_names.append(class_name)

        for child in node.children:
            self._traverse_for_class_declarations(child, class_names)

    def _get_superclass_name(self, class_qn: str) -> str | None:
        """Get the superclass name using precise tree-sitter AST analysis."""
        parts = class_qn.split(SEPARATOR_DOT)
        if len(parts) < 2:
            return None

        module_qn = SEPARATOR_DOT.join(parts[:-1])
        target_class_name = parts[-1]

        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is None or file_path not in self.ast_cache:
            return None

        root_node, _ = self.ast_cache[file_path]

        superclass = self._find_superclass_using_ast(
            root_node, target_class_name, module_qn
        )
        return superclass

    def _find_superclass_using_ast(
        self, node: Node, target_class_name: str, module_qn: str
    ) -> str | None:
        """Find superclass using precise tree-sitter AST traversal."""
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node and safe_decode_text(name_node) == target_class_name:
                superclass_node = node.child_by_field_name("superclass")
                if superclass_node:
                    type_node = superclass_node.child_by_field_name("type")
                    if type_node:
                        superclass_name = safe_decode_text(type_node)
                        if superclass_name:
                            return self._resolve_java_type_name(
                                superclass_name, module_qn
                            )

        for child in node.children:
            result = self._find_superclass_using_ast(
                child, target_class_name, module_qn
            )
            if result:
                return result

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

        for child in node.children:
            self._traverse_for_enhanced_for_loops(child, local_var_types, module_qn)

    def _process_enhanced_for_statement(
        self, for_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        """Process enhanced for statement using tree-sitter field access."""
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
            for child in for_node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        var_name = safe_decode_text(name_node)
                        if var_name:
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

    def _find_field_type_in_class(
        self, root_node: Node, class_name: str, field_name: str, module_qn: str
    ) -> str | None:
        """Find the type of a specific field in a class using tree-sitter AST analysis."""

        for child in root_node.children:
            if child.type == "class_declaration":
                class_info = extract_java_class_info(child)
                if class_info.get("name") == class_name:
                    class_body = child.child_by_field_name("body")
                    if class_body:
                        for field_child in class_body.children:
                            if field_child.type == "field_declaration":
                                field_info = extract_java_field_info(field_child)
                                if field_info.get("name") == field_name:
                                    field_type = field_info.get("type")
                                    if field_type:
                                        return self._resolve_java_type_name(
                                            str(field_type), module_qn
                                        )
        return None
