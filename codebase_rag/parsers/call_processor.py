"""Call processor for finding and resolving function/method calls."""

import re
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..language_config import LanguageConfig
from ..services import IngestorProtocol

# No longer need constants import - using Tree-sitter directly
from .cpp_utils import convert_operator_symbol_to_name, extract_cpp_function_name
from .import_processor import ImportProcessor
from .python_utils import resolve_class_name
from .type_inference import TypeInferenceEngine


class CallProcessor:
    """Handles processing of function and method calls."""

    # JavaScript built-in types for type inference
    _JS_BUILTIN_TYPES = {
        "Array",
        "Object",
        "String",
        "Number",
        "Date",
        "RegExp",
        "Function",
        "Map",
        "Set",
        "Promise",
        "Error",
        "Boolean",
    }

    # JavaScript built-in patterns for static method calls
    _JS_BUILTIN_PATTERNS = {
        # Object static methods
        "Object.create",
        "Object.keys",
        "Object.values",
        "Object.entries",
        "Object.assign",
        "Object.freeze",
        "Object.seal",
        "Object.defineProperty",
        "Object.getPrototypeOf",
        "Object.setPrototypeOf",
        # Array static methods
        "Array.from",
        "Array.of",
        "Array.isArray",
        # Global functions
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "encodeURIComponent",
        "decodeURIComponent",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
        # Console methods
        "console.log",
        "console.error",
        "console.warn",
        "console.info",
        "console.debug",
        # JSON methods
        "JSON.parse",
        "JSON.stringify",
        # Math static methods
        "Math.random",
        "Math.floor",
        "Math.ceil",
        "Math.round",
        "Math.abs",
        "Math.max",
        "Math.min",
        # Date static methods
        "Date.now",
        "Date.parse",
    }

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: Any,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.class_inheritance = class_inheritance

    def process_calls_in_file(
        self, file_path: Path, root_node: Node, language: str, queries: dict[str, Any]
    ) -> None:
        """Process function calls in a specific file using its cached AST."""
        relative_path = file_path.relative_to(self.repo_path)
        logger.debug(f"Processing calls in cached AST for: {relative_path}")

        try:
            module_qn = ".".join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name in ("__init__.py", "mod.rs"):
                # In Python, __init__.py and in Rust, mod.rs represent the parent module directory
                module_qn = ".".join(
                    [self.project_name] + list(relative_path.parent.parts)
                )

            self._process_calls_in_functions(root_node, module_qn, language, queries)
            self._process_calls_in_classes(root_node, module_qn, language, queries)
            self._process_module_level_calls(root_node, module_qn, language, queries)

        except Exception as e:
            logger.error(f"Failed to process calls in {file_path}: {e}")

    def _process_calls_in_functions(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Process calls within top-level functions."""
        lang_queries = queries[language]
        lang_config: LanguageConfig = lang_queries["config"]

        query = lang_queries["functions"]
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        func_nodes = captures.get("function", [])
        for func_node in func_nodes:
            if not isinstance(func_node, Node):
                continue
            if self._is_method(func_node, lang_config):
                continue

            # Extract function name using appropriate method for language
            if language == "cpp":
                # For C++, use utility functions instead of creating a temporary instance
                func_name = extract_cpp_function_name(func_node)
                if not func_name:
                    continue
            else:
                name_node = func_node.child_by_field_name("name")
                if not name_node:
                    continue
                text = name_node.text
                if text is None:
                    continue
                func_name = text.decode("utf8")
            func_qn = self._build_nested_qualified_name(
                func_node, module_qn, func_name, lang_config
            )

            if func_qn:
                self._ingest_function_calls(
                    func_node, func_qn, "Function", module_qn, language, queries
                )

    def _process_calls_in_classes(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Process calls within class methods."""
        lang_queries = queries[language]
        if not lang_queries.get("classes"):
            return

        query = lang_queries["classes"]
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        class_nodes = captures.get("class", [])

        for class_node in class_nodes:
            if not isinstance(class_node, Node):
                continue

            # Rust impl blocks don't have a "name" field, they have a "type" field
            if language == "rust" and class_node.type == "impl_item":
                # For Rust impl blocks, get the type being implemented
                type_node = class_node.child_by_field_name("type")
                if not type_node:
                    # Might be a type_identifier child directly
                    for child in class_node.children:
                        if child.type == "type_identifier" and child.is_named:
                            type_node = child
                            break
                if not type_node or not type_node.text:
                    continue
                class_name = type_node.text.decode("utf8")
                class_qn = f"{module_qn}.{class_name}"
            else:
                # Standard class handling for other languages
                name_node = class_node.child_by_field_name("name")
                if not name_node:
                    continue
                text = name_node.text
                if text is None:
                    continue
                class_name = text.decode("utf8")
                class_qn = f"{module_qn}.{class_name}"

            body_node = class_node.child_by_field_name("body")
            if not body_node:
                continue

            method_query = lang_queries["functions"]
            method_cursor = QueryCursor(method_query)
            method_captures = method_cursor.captures(body_node)
            method_nodes = method_captures.get("function", [])
            for method_node in method_nodes:
                if not isinstance(method_node, Node):
                    continue
                method_name_node = method_node.child_by_field_name("name")
                if not method_name_node:
                    continue
                text = method_name_node.text
                if text is None:
                    continue
                method_name = text.decode("utf8")
                method_qn = f"{class_qn}.{method_name}"

                self._ingest_function_calls(
                    method_node,
                    method_qn,
                    "Method",
                    module_qn,
                    language,
                    queries,
                    class_qn,
                )

    def _process_module_level_calls(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Process top-level calls in the module (like IIFE calls)."""
        # Process calls that are directly at module level, not inside functions/classes
        self._ingest_function_calls(
            root_node, module_qn, "Module", module_qn, language, queries
        )

    def _get_call_target_name(self, call_node: Node) -> str | None:
        """Extracts the name of the function or method being called."""
        # For 'call' in Python and 'call_expression' in JS/TS/C++
        if func_child := call_node.child_by_field_name("function"):
            if func_child.type == "identifier":
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            # Python: obj.method() -> attribute
            elif func_child.type == "attribute":
                # Return the full attribute path
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            # JS/TS: obj.method() -> member_expression
            elif func_child.type == "member_expression":
                # Return the full member expression (e.g., "obj.method")
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            # C++: obj.method() -> field_expression
            elif func_child.type == "field_expression":
                # Extract method name from field_expression
                field_node = func_child.child_by_field_name("field")
                if field_node and field_node.text:
                    return str(field_node.text.decode("utf8"))
            # C++: namespace::func() or Class::method() -> qualified_identifier
            elif func_child.type == "qualified_identifier":
                # Return the full qualified name (e.g., "std::cout")
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            # Rust: Type::method() or module::func() -> scoped_identifier
            elif func_child.type == "scoped_identifier":
                # Return the full scoped name (e.g., "Storage::get_instance")
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            # JS/TS: IIFE calls like (function(){})() -> parenthesized_expression
            elif func_child.type == "parenthesized_expression":
                # For IIFEs, we need to identify the anonymous function inside
                return self._get_iife_target_name(func_child)

        # C++: Binary operators like obj1 + obj2 -> operator+
        if call_node.type == "binary_expression":
            # Use Tree-sitter field access to get the operator directly
            operator_node = call_node.child_by_field_name("operator")
            if operator_node and operator_node.text:
                operator_text = operator_node.text.decode("utf8")
                return convert_operator_symbol_to_name(operator_text)

        # C++: Unary operators like ++obj, --obj -> operator++, operator--
        if call_node.type in ["unary_expression", "update_expression"]:
            # Use Tree-sitter field access to get the operator directly
            operator_node = call_node.child_by_field_name("operator")
            if operator_node and operator_node.text:
                operator_text = operator_node.text.decode("utf8")
                return convert_operator_symbol_to_name(operator_text)

        # For 'method_invocation' in Java
        if call_node.type == "method_invocation":
            # Get the object (receiver) part
            object_node = call_node.child_by_field_name("object")
            name_node = call_node.child_by_field_name("name")

            if name_node and name_node.text:
                method_name = str(name_node.text.decode("utf8"))

                if object_node and object_node.text:
                    object_text = str(object_node.text.decode("utf8"))
                    return f"{object_text}.{method_name}"
                else:
                    # No object, likely this.method() or static method
                    return method_name

        # General case for other languages
        if name_node := call_node.child_by_field_name("name"):
            text = name_node.text
            if text is not None:
                return str(text.decode("utf8"))

        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        """Extract the target name for IIFE calls like (function(){})()."""
        # Look for function_expression or arrow_function inside parentheses
        for child in parenthesized_expr.children:
            if child.type in ["function_expression", "arrow_function"]:
                # Generate the same synthetic name that was used during function detection
                if child.type == "arrow_function":
                    return f"iife_arrow_{child.start_point[0]}_{child.start_point[1]}"
                else:
                    return f"iife_func_{child.start_point[0]}_{child.start_point[1]}"
        return None

    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: str,
        queries: dict[str, Any],
        class_context: str | None = None,
    ) -> None:
        """Find and ingest function calls within a caller node."""
        calls_query = queries[language].get("calls")
        if not calls_query:
            return

        local_var_types = self.type_inference.build_local_variable_type_map(
            caller_node, module_qn, language
        )

        cursor = QueryCursor(calls_query)
        captures = cursor.captures(caller_node)
        call_nodes = captures.get("call", [])

        logger.debug(
            f"Found {len(call_nodes)} call nodes in {language} for {caller_qn}"
        )

        for call_node in call_nodes:
            if not isinstance(call_node, Node):
                continue

            # Process nested calls first (inner to outer)
            self._process_nested_calls_in_node(
                call_node,
                caller_qn,
                caller_type,
                module_qn,
                local_var_types,
                class_context,
            )

            call_name = self._get_call_target_name(call_node)
            if not call_name:
                continue

            # Use Java-specific resolution for Java method calls
            if language == "java" and call_node.type == "method_invocation":
                callee_info = self._resolve_java_method_call(
                    call_node, module_qn, local_var_types
                )
            else:
                callee_info = self._resolve_function_call(
                    call_name, module_qn, local_var_types, class_context
                )
            if not callee_info:
                # Check if it's a built-in JavaScript method
                builtin_info = self._resolve_builtin_call(call_name)
                if not builtin_info:
                    # Check if it's a C++ operator
                    operator_info = self._resolve_cpp_operator_call(
                        call_name, module_qn
                    )
                    if not operator_info:
                        continue
                    callee_type, callee_qn = operator_info
                else:
                    callee_type, callee_qn = builtin_info
            else:
                callee_type, callee_qn = callee_info
            logger.debug(
                f"      Found call from {caller_qn} to {call_name} "
                f"(resolved as {callee_type}:{callee_qn})"
            )

            # NOTE: We don't call ensure_node_batch here because all Function/Method/Class
            # nodes are already created in Pass 2 (definition processing) before we reach
            # Pass 3 (call processing). Re-creating nodes here would overwrite their
            # properties (decorators, line numbers, docstrings, etc.) with minimal data.
            self.ingestor.ensure_relationship_batch(
                (caller_type, "qualified_name", caller_qn),
                "CALLS",
                (callee_type, "qualified_name", callee_qn),
            )

    def _process_nested_calls_in_node(
        self,
        call_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        """Process nested call expressions within a call node's function expression."""
        # Get the function expression of this call
        func_child = call_node.child_by_field_name("function")
        if not func_child:
            return

        # If the function is an attribute (e.g., obj.method), check if obj contains calls
        if func_child.type == "attribute":
            # Recursively search for nested calls in the object part
            self._find_and_process_nested_calls(
                func_child,
                caller_qn,
                caller_type,
                module_qn,
                local_var_types,
                class_context,
            )

    def _find_and_process_nested_calls(
        self,
        node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        """Recursively find and process call expressions in a node tree."""
        # If this node is a call expression, process it
        if node.type == "call":
            # First process any nested calls within this call
            self._process_nested_calls_in_node(
                node, caller_qn, caller_type, module_qn, local_var_types, class_context
            )

            # Then process this call itself
            call_name = self._get_call_target_name(node)
            if call_name:
                callee_info = self._resolve_function_call(
                    call_name, module_qn, local_var_types, class_context
                )
                if callee_info:
                    callee_type, callee_qn = callee_info
                    logger.debug(
                        f"      Found nested call from {caller_qn} to {call_name} "
                        f"(resolved as {callee_type}:{callee_qn})"
                    )
                    # NOTE: We don't call ensure_node_batch here - nodes already exist from Pass 2
                    self.ingestor.ensure_relationship_batch(
                        (caller_type, "qualified_name", caller_qn),
                        "CALLS",
                        (callee_type, "qualified_name", callee_qn),
                    )

        # Recursively search in all child nodes
        for child in node.children:
            self._find_and_process_nested_calls(
                child, caller_qn, caller_type, module_qn, local_var_types, class_context
            )

    def _resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
    ) -> tuple[str, str] | None:
        """Resolve a function call to its qualified name and type."""
        # Phase -1: Handle IIFE calls specially
        if call_name and (
            call_name.startswith("iife_func_") or call_name.startswith("iife_arrow_")
        ):
            # IIFE calls: resolve to the anonymous function in the same module
            iife_qn = f"{module_qn}.{call_name}"
            if iife_qn in self.function_registry:
                return self.function_registry[iife_qn], iife_qn

        # Phase 0: Handle super calls specially (JavaScript/TypeScript patterns)
        if (
            call_name == "super"
            or call_name.startswith("super.")
            or call_name.startswith("super()")
        ):
            return self._resolve_super_call(call_name, module_qn, class_context)

        # Phase 0.5: Handle method chaining - check if this is a chained call
        if "." in call_name and self._is_method_chain(call_name):
            return self._resolve_chained_call(call_name, module_qn, local_var_types)

        # Phase 1: Check import mapping for 100% accurate resolution
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]

            # 1a.1. Direct import resolution
            if call_name in import_map:
                imported_qn = import_map[call_name]
                if imported_qn in self.function_registry:
                    logger.debug(
                        f"Direct import resolved: {call_name} -> {imported_qn}"
                    )
                    return self.function_registry[imported_qn], imported_qn

            # 1a.2. Handle qualified calls like "Class.method", "self.attr.method", C++ "Class::method", and Lua "object:method"
            if "." in call_name or "::" in call_name or ":" in call_name:
                # Split by '::' for C++, ':' for Lua, or '.' for other languages
                if "::" in call_name:
                    separator = "::"
                elif ":" in call_name:
                    separator = ":"
                else:
                    separator = "."
                parts = call_name.split(separator)

                # Handle JavaScript object method calls like "calculator.add"
                if len(parts) == 2:
                    object_name, method_name = parts

                    # First try type inference if available
                    if local_var_types and object_name in local_var_types:
                        var_type = local_var_types[object_name]

                        # Check if var_type is already fully qualified (contains dots)
                        if "." in var_type:
                            class_qn = var_type
                        # Resolve var_type to full qualified name
                        elif var_type in import_map:
                            class_qn = import_map[var_type]
                        else:
                            class_qn_or_none = self._resolve_class_name(
                                var_type, module_qn
                            )
                            class_qn = class_qn_or_none if class_qn_or_none else ""

                        if class_qn:
                            # Use the same separator that was used in the call (: for Lua, . for others)
                            method_qn = f"{class_qn}{separator}{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    f"Type-inferred object method resolved: "
                                    f"{call_name} -> {method_qn} "
                                    f"(via {object_name}:{var_type})"
                                )
                                return self.function_registry[method_qn], method_qn

                            # Check inheritance for this method
                            inherited_method = self._resolve_inherited_method(
                                class_qn, method_name
                            )
                            if inherited_method:
                                logger.debug(
                                    f"Type-inferred inherited object method resolved: "
                                    f"{call_name} -> {inherited_method[1]} "
                                    f"(via {object_name}:{var_type})"
                                )
                                return inherited_method

                        # Check if this is a built-in JavaScript type
                        if var_type in self._JS_BUILTIN_TYPES:
                            # This is a built-in type instance method call
                            return (
                                "Function",
                                f"builtin.{var_type}.prototype.{method_name}",
                            )

                    # Fallback 1: Try treating object_name as a class name (for static methods like Storage::getInstance or Storage:getInstance)
                    if object_name in import_map:
                        class_qn = import_map[object_name]

                        # For Rust, imports use :: separators (e.g., "controllers::SceneController")
                        # Convert to project-qualified names (e.g., "rust_proj.src.controllers.SceneController")
                        if "::" in class_qn:
                            # Extract last component as class name
                            rust_parts = class_qn.split("::")
                            class_name = rust_parts[-1]

                            # Scan registry entries that end with this class name (linear helper)
                            matching_qns = self.function_registry.find_ending_with(
                                class_name
                            )
                            # Find the first Class entry
                            for qn in matching_qns:
                                if self.function_registry.get(qn) == "Class":
                                    class_qn = qn
                                    break

                        # For languages like JavaScript/Lua, imports may point to modules
                        # but the class/table has the same name inside: e.g., storage.Storage -> storage.Storage.Storage
                        # Check if methods exist with this extended class path
                        potential_class_qn = f"{class_qn}.{object_name}"
                        test_method_qn = f"{potential_class_qn}{separator}{method_name}"
                        if test_method_qn in self.function_registry:
                            # The extended path works, use it
                            class_qn = potential_class_qn

                        # Construct method QN using the appropriate separator
                        # For Lua, use : for both instance and class methods (Lua tables)
                        # For Rust/C++, use . for registry lookup (even though call uses ::)
                        # For others, use . for static/class methods
                        registry_separator = separator if separator == ":" else "."
                        method_qn = f"{class_qn}{registry_separator}{method_name}"
                        if method_qn in self.function_registry:
                            logger.debug(
                                f"Import-resolved static call: {call_name} -> {method_qn}"
                            )
                            return self.function_registry[method_qn], method_qn

                    # Fallback 2: Try to find the method in the same module
                    method_qn = f"{module_qn}.{method_name}"
                    if method_qn in self.function_registry:
                        logger.debug(
                            f"Object method resolved: {call_name} -> {method_qn}"
                        )
                        return self.function_registry[method_qn], method_qn

                # Special handling for self.attribute.method patterns
                if len(parts) >= 3 and parts[0] == "self":
                    attribute_ref = ".".join(parts[:-1])  # "self.repo"
                    method_name = parts[-1]  # "find_by_id"

                    # Check if we have type info for this attribute reference
                    if local_var_types and attribute_ref in local_var_types:
                        var_type = local_var_types[attribute_ref]

                        # Check if var_type is already fully qualified (contains dots)
                        if "." in var_type:
                            class_qn = var_type
                        # Resolve var_type to full qualified name
                        elif var_type in import_map:
                            class_qn = import_map[var_type]
                        else:
                            class_qn_or_none = self._resolve_class_name(
                                var_type, module_qn
                            )
                            class_qn = class_qn_or_none if class_qn_or_none else ""

                        if class_qn:
                            # For self.attribute.method, the method separator is always . (not : or ::)
                            method_qn = f"{class_qn}.{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    f"Instance-resolved self-attribute call: "
                                    f"{call_name} -> {method_qn} "
                                    f"(via {attribute_ref}:{var_type})"
                                )
                                return self.function_registry[method_qn], method_qn

                            # Check inheritance for this method
                            inherited_method = self._resolve_inherited_method(
                                class_qn, method_name
                            )
                            if inherited_method:
                                logger.debug(
                                    f"Instance-resolved inherited self-attribute call: "
                                    f"{call_name} -> {inherited_method[1]} "
                                    f"(via {attribute_ref}:{var_type})"
                                )
                                return inherited_method
                else:
                    # Regular Class.method pattern
                    class_name = parts[0]
                    method_name = ".".join(parts[1:])

                    # Check if the class is imported
                    if class_name in import_map:
                        class_qn = import_map[class_name]
                        method_qn = f"{class_qn}.{method_name}"
                        if method_qn in self.function_registry:
                            logger.debug(
                                f"Import-resolved qualified call: "
                                f"{call_name} -> {method_qn}"
                            )
                            return self.function_registry[method_qn], method_qn

                    # Then, check if the base is a local variable with known type
                    if local_var_types and class_name in local_var_types:
                        var_type = local_var_types[class_name]

                        # Check if var_type is already fully qualified (contains dots)
                        if "." in var_type:
                            class_qn = var_type
                        # The var_type might be a simple class name, resolve to full qn
                        elif var_type in import_map:
                            class_qn = import_map[var_type]
                        else:
                            # Try to find the class in the same module or resolve it
                            class_qn_or_none = self._resolve_class_name(
                                var_type, module_qn
                            )
                            class_qn = class_qn_or_none if class_qn_or_none else ""

                        if class_qn:
                            method_qn = f"{class_qn}.{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    f"Instance-resolved qualified call: "
                                    f"{call_name} -> {method_qn} "
                                    f"(via {class_name}:{var_type})"
                                )
                                return self.function_registry[method_qn], method_qn

                            # If method not found in the class, check inheritance chain
                            inherited_method = self._resolve_inherited_method(
                                class_qn, method_name
                            )
                            if inherited_method:
                                inherited_method_qn = inherited_method[1]
                                logger.debug(
                                    f"Instance-resolved inherited call: "
                                    f"{call_name} -> {inherited_method_qn} "
                                    f"(via {class_name}:{var_type})"
                                )
                                return inherited_method

            # 1b. Check wildcard imports
            for local_name, imported_qn in import_map.items():
                if local_name.startswith("*"):
                    # Construct potential qualified name from wildcard import
                    potential_qns = []

                    # If the imported_qn contains '::', use '::' separator
                    if "::" in imported_qn:
                        potential_qns.append(f"{imported_qn}::{call_name}")
                    else:
                        # For languages like Java/Python/Scala that use '.'
                        potential_qns.append(f"{imported_qn}.{call_name}")
                        # Also try '::' in case the imported name is C++ style
                        potential_qns.append(f"{imported_qn}::{call_name}")

                    for wildcard_qn in potential_qns:
                        if wildcard_qn in self.function_registry:
                            logger.debug(
                                f"Wildcard-resolved call: {call_name} -> {wildcard_qn}"
                            )
                            return self.function_registry[wildcard_qn], wildcard_qn

        # Phase 2: Heuristic-based resolution (less accurate but often effective)
        # 2a. Check for a function in the same module
        same_module_func_qn = f"{module_qn}.{call_name}"
        if same_module_func_qn in self.function_registry:
            logger.debug(
                f"Same-module resolution: {call_name} -> {same_module_func_qn}"
            )
            return (
                self.function_registry[same_module_func_qn],
                same_module_func_qn,
            )

        # 2b. Use the Trie to find any matching symbol
        # This is a fallback and can be imprecise, but better than nothing.
        # For qualified calls like "Class.method", "Class::method", or "object:method", extract just the method name
        search_name = re.split(r"[.:]|::", call_name)[-1]

        possible_matches = self.function_registry.find_ending_with(search_name)
        if possible_matches:
            # Sort candidates by likelihood (prioritize closer modules)
            possible_matches.sort(
                key=lambda qn: self._calculate_import_distance(qn, module_qn)
            )
            # Take the most likely candidate.
            best_candidate_qn = possible_matches[0]
            logger.debug(
                f"Trie-based fallback resolution: {call_name} -> {best_candidate_qn}"
            )
            return (
                self.function_registry[best_candidate_qn],
                best_candidate_qn,
            )

        logger.debug(f"Could not resolve call: {call_name}")
        return None

    def _resolve_builtin_call(self, call_name: str) -> tuple[str, str] | None:
        """Resolve built-in JavaScript method calls that don't exist in user code."""
        # Common built-in JavaScript objects and their methods
        # Check if the call matches any built-in pattern
        if call_name in self._JS_BUILTIN_PATTERNS:
            return ("Function", f"builtin.{call_name}")

        # Note: Instance method calls on built-in objects (e.g., myArray.push)
        # are now handled via type inference in _resolve_function_call

        # Handle JavaScript function binding methods (.bind, .call, .apply)
        if (
            call_name.endswith(".bind")
            or call_name.endswith(".call")
            or call_name.endswith(".apply")
        ):
            # These are special JavaScript method binding calls
            # Track them as function calls to the binding methods themselves
            if call_name.endswith(".bind"):
                return ("Function", "builtin.Function.prototype.bind")
            elif call_name.endswith(".call"):
                return ("Function", "builtin.Function.prototype.call")
            elif call_name.endswith(".apply"):
                return ("Function", "builtin.Function.prototype.apply")

        # Handle prototype method calls with .call or .apply
        if ".prototype." in call_name and (
            call_name.endswith(".call") or call_name.endswith(".apply")
        ):
            # Extract the prototype method name without .call/.apply
            base_call = call_name.rsplit(".", 1)[0]  # Remove .call or .apply
            return ("Function", base_call)

        return None

    def _resolve_cpp_operator_call(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Resolve C++ operator calls to built-in operator functions."""
        if not call_name.startswith("operator"):
            return None

        # Map C++ operators to standardized function names
        cpp_operators = {
            "operator_plus": "builtin.cpp.operator_plus",
            "operator_minus": "builtin.cpp.operator_minus",
            "operator_multiply": "builtin.cpp.operator_multiply",
            "operator_divide": "builtin.cpp.operator_divide",
            "operator_modulo": "builtin.cpp.operator_modulo",
            "operator_equal": "builtin.cpp.operator_equal",
            "operator_not_equal": "builtin.cpp.operator_not_equal",
            "operator_less": "builtin.cpp.operator_less",
            "operator_greater": "builtin.cpp.operator_greater",
            "operator_less_equal": "builtin.cpp.operator_less_equal",
            "operator_greater_equal": "builtin.cpp.operator_greater_equal",
            "operator_assign": "builtin.cpp.operator_assign",
            "operator_plus_assign": "builtin.cpp.operator_plus_assign",
            "operator_minus_assign": "builtin.cpp.operator_minus_assign",
            "operator_multiply_assign": "builtin.cpp.operator_multiply_assign",
            "operator_divide_assign": "builtin.cpp.operator_divide_assign",
            "operator_modulo_assign": "builtin.cpp.operator_modulo_assign",
            "operator_increment": "builtin.cpp.operator_increment",
            "operator_decrement": "builtin.cpp.operator_decrement",
            "operator_left_shift": "builtin.cpp.operator_left_shift",
            "operator_right_shift": "builtin.cpp.operator_right_shift",
            "operator_bitwise_and": "builtin.cpp.operator_bitwise_and",
            "operator_bitwise_or": "builtin.cpp.operator_bitwise_or",
            "operator_bitwise_xor": "builtin.cpp.operator_bitwise_xor",
            "operator_bitwise_not": "builtin.cpp.operator_bitwise_not",
            "operator_logical_and": "builtin.cpp.operator_logical_and",
            "operator_logical_or": "builtin.cpp.operator_logical_or",
            "operator_logical_not": "builtin.cpp.operator_logical_not",
            "operator_subscript": "builtin.cpp.operator_subscript",
            "operator_call": "builtin.cpp.operator_call",
        }

        if call_name in cpp_operators:
            return ("Function", cpp_operators[call_name])

        # Handle custom/overloaded operators in the same module
        # Try to find a matching operator definition
        possible_matches = self.function_registry.find_ending_with(call_name)
        if possible_matches:
            # Prefer operators from the same module
            same_module_ops = [
                qn
                for qn in possible_matches
                if qn.startswith(module_qn) and call_name in qn
            ]
            if same_module_ops:
                # Sort to ensure deterministic selection, preferring shorter QNs
                same_module_ops.sort(key=lambda qn: (len(qn), qn))
                best_candidate = same_module_ops[0]
                return (self.function_registry[best_candidate], best_candidate)

            # Fallback to any matching operator
            # Sort to ensure deterministic selection
            possible_matches.sort(key=lambda qn: (len(qn), qn))
            best_candidate = possible_matches[0]
            return (self.function_registry[best_candidate], best_candidate)

        return None

    def _is_method_chain(self, call_name: str) -> bool:
        """Check if this appears to be a method chain with parentheses (not just obj.method)."""
        # Look for patterns like: obj.method().other_method or obj.method("arg").other_method
        # But not simple patterns like: obj.method or self.attr
        if "(" in call_name and ")" in call_name:
            # Count method calls - if more than one, it's likely chaining
            parts = call_name.split(".")
            method_calls = sum(1 for part in parts if "(" in part and ")" in part)
            return method_calls >= 1 and len(parts) >= 2
        return False

    def _resolve_chained_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        """Resolve chained method calls like obj.method().other_method()."""
        # For chained calls like "processed_user.update_name('Updated').clone"
        # We need to resolve the return type of the inner call first

        # Handle the case where we have method(args).method format
        # Find the rightmost method that's not in parentheses

        # Pattern to find the final method call: anything.method
        # where method is at the end and not in parentheses
        match = re.search(r"\.([^.()]+)$", call_name)
        if not match:
            return None

        final_method = match.group(1)

        # Get the object expression (everything before the final method)
        object_expr = call_name[: match.start()]

        # Try to get the return type of the object expression
        object_type = self.type_inference._infer_expression_return_type(
            object_expr, module_qn, local_var_types
        )

        if object_type:
            # Convert object_type to full qualified name if it's a short name
            full_object_type = object_type
            if "." not in object_type:
                # This is a short class name, resolve to full qualified name
                resolved_class = self._resolve_class_name(object_type, module_qn)
                if resolved_class:
                    full_object_type = resolved_class

            # Now resolve the final method call on that type
            method_qn = f"{full_object_type}.{final_method}"

            if method_qn in self.function_registry:
                logger.debug(
                    f"Resolved chained call: {call_name} -> {method_qn} "
                    f"(via {object_expr}:{object_type})"
                )
                return self.function_registry[method_qn], method_qn

            # Also check inheritance for the final method
            inherited_method = self._resolve_inherited_method(
                full_object_type, final_method
            )
            if inherited_method:
                logger.debug(
                    f"Resolved chained inherited call: {call_name} -> {inherited_method[1]} "
                    f"(via {object_expr}:{object_type})"
                )
                return inherited_method

        return None

    def _resolve_super_call(
        self, call_name: str, module_qn: str, class_context: str | None = None
    ) -> tuple[str, str] | None:
        """Resolve super calls to parent class methods (JavaScript/TypeScript patterns)."""
        # Extract method name from super call
        # JavaScript patterns:
        # - "super" -> constructor call: super(args)
        # - "super.method" -> parent method call: super.method()
        # - "super().method" -> chained call (less common)

        if call_name == "super":
            # Constructor call: super(args)
            method_name = "constructor"
        elif call_name.startswith("super."):
            # Method call: super.method()
            method_name = call_name.split(".", 1)[1]  # Get part after "super."
        elif "." in call_name:
            # Legacy pattern: super().method()
            method_name = call_name.split(".", 1)[1]  # Get part after "super()."
        else:
            # Unsupported pattern
            return None

        # Use the provided class context
        current_class_qn = class_context
        if not current_class_qn:
            logger.debug(f"No class context provided for super() call: {call_name}")
            return None

        # Look up parent classes for the current class
        if current_class_qn not in self.class_inheritance:
            logger.debug(f"No inheritance info for class {current_class_qn}")
            return None

        parent_classes = self.class_inheritance[current_class_qn]
        if not parent_classes:
            logger.debug(f"No parent classes found for {current_class_qn}")
            return None

        # Use inheritance chain traversal to find the method
        result = self._resolve_inherited_method(current_class_qn, method_name)
        if result:
            callee_type, parent_method_qn = result
            logger.debug(f"Resolved super() call: {call_name} -> {parent_method_qn}")
            return callee_type, parent_method_qn

        logger.debug(
            f"Could not resolve super() call: {call_name} in parents of {current_class_qn}"
        )
        return None

    def _resolve_inherited_method(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None:
        """Resolve a method by looking up the inheritance chain."""
        # Check if we have inheritance information for this class
        if class_qn not in self.class_inheritance:
            return None

        # Use a queue for breadth-first search through the inheritance hierarchy
        queue = list(self.class_inheritance.get(class_qn, []))
        visited = set(queue)

        while queue:
            parent_class_qn = queue.pop(0)
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            # Check if the method exists in the current parent class
            if parent_method_qn in self.function_registry:
                return (
                    self.function_registry[parent_method_qn],
                    parent_method_qn,
                )

            # Add the parent's parents to the queue for further searching
            if parent_class_qn in self.class_inheritance:
                for grandparent_qn in self.class_inheritance[parent_class_qn]:
                    if grandparent_qn not in visited:
                        visited.add(grandparent_qn)
                        queue.append(grandparent_qn)

        return None

    def _calculate_import_distance(
        self, candidate_qn: str, caller_module_qn: str
    ) -> int:
        """
        Calculate the 'distance' between a candidate function and the calling module.
        Lower values indicate more likely imports (closer modules, common prefixes).
        """
        caller_parts = caller_module_qn.split(".")
        candidate_parts = candidate_qn.split(".")

        # Find common prefix length (how many package levels they share)
        common_prefix = 0
        for i in range(min(len(caller_parts), len(candidate_parts))):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break

        # Calculate base distance (inverse of common prefix)
        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

        # Bonus for candidates that are "close" in the module hierarchy
        if candidate_qn.startswith(".".join(caller_parts[:-1]) + "."):
            base_distance -= 1

        return base_distance

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        """Convert a simple class name to its fully qualified name."""
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageConfig,
    ) -> str | None:
        """Build qualified name for nested functions."""
        path_parts = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                f"Unexpected parent type for node {func_node}: {type(current)}. "
                f"Skipping."
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode("utf8"))
            elif current.type in lang_config.class_node_types:
                return None  # This is a method

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{func_name}"
        else:
            return f"{module_qn}.{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageConfig) -> bool:
        """Check if a function is actually a method inside a class."""
        current = func_node.parent
        if not isinstance(current, Node):
            return False

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.class_node_types:
                return True
            current = current.parent
        return False

    def _resolve_java_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str],
    ) -> tuple[str, str] | None:
        """Resolve Java method calls using the JavaTypeInferenceEngine."""
        # Get the Java type inference engine from the main type inference engine
        java_engine = self.type_inference.java_type_inference

        # Use the Java engine to resolve the method call
        result = java_engine.resolve_java_method_call(
            call_node, local_var_types, module_qn
        )

        if result:
            logger.debug(
                f"Java method call resolved: {call_node.text.decode('utf8') if call_node.text else 'unknown'} -> {result[1]}"
            )

        return result
