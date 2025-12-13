import re
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..language_config import LanguageConfig
from ..services import IngestorProtocol
from .cpp_utils import convert_operator_symbol_to_name, extract_cpp_function_name
from .import_processor import ImportProcessor
from .python_utils import resolve_class_name
from .type_inference import TypeInferenceEngine


class CallProcessor:
    """Handles processing of function and method calls."""

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

    _JS_BUILTIN_PATTERNS = {
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
        "Array.from",
        "Array.of",
        "Array.isArray",
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
        "console.log",
        "console.error",
        "console.warn",
        "console.info",
        "console.debug",
        "JSON.parse",
        "JSON.stringify",
        "Math.random",
        "Math.floor",
        "Math.ceil",
        "Math.round",
        "Math.abs",
        "Math.max",
        "Math.min",
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

            if language == "cpp":
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

            if language == "rust" and class_node.type == "impl_item":
                type_node = class_node.child_by_field_name("type")
                if not type_node:
                    for child in class_node.children:
                        if child.type == "type_identifier" and child.is_named:
                            type_node = child
                            break
                if not type_node or not type_node.text:
                    continue
                class_name = type_node.text.decode("utf8")
                class_qn = f"{module_qn}.{class_name}"
            else:
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
        self._ingest_function_calls(
            root_node, module_qn, "Module", module_qn, language, queries
        )

    def _get_call_target_name(self, call_node: Node) -> str | None:
        """Extracts the name of the function or method being called."""
        if func_child := call_node.child_by_field_name("function"):
            if func_child.type == "identifier":
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            elif func_child.type == "attribute":
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            elif func_child.type == "member_expression":
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            elif func_child.type == "field_expression":
                field_node = func_child.child_by_field_name("field")
                if field_node and field_node.text:
                    return str(field_node.text.decode("utf8"))
            elif func_child.type == "qualified_identifier":
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            elif func_child.type == "scoped_identifier":
                text = func_child.text
                if text is not None:
                    return str(text.decode("utf8"))
            elif func_child.type == "parenthesized_expression":
                return self._get_iife_target_name(func_child)

        if call_node.type == "binary_expression":
            operator_node = call_node.child_by_field_name("operator")
            if operator_node and operator_node.text:
                operator_text = operator_node.text.decode("utf8")
                return convert_operator_symbol_to_name(operator_text)

        if call_node.type in ["unary_expression", "update_expression"]:
            operator_node = call_node.child_by_field_name("operator")
            if operator_node and operator_node.text:
                operator_text = operator_node.text.decode("utf8")
                return convert_operator_symbol_to_name(operator_text)

        if call_node.type == "method_invocation":
            object_node = call_node.child_by_field_name("object")
            name_node = call_node.child_by_field_name("name")

            if name_node and name_node.text:
                method_name = str(name_node.text.decode("utf8"))

                if object_node and object_node.text:
                    object_text = str(object_node.text.decode("utf8"))
                    return f"{object_text}.{method_name}"
                else:
                    return method_name

        if name_node := call_node.child_by_field_name("name"):
            text = name_node.text
            if text is not None:
                return str(text.decode("utf8"))

        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        """Extract the target name for IIFE calls like (function(){})()."""
        for child in parenthesized_expr.children:
            if child.type in ["function_expression", "arrow_function"]:
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

            # NOTE: We removed _process_nested_calls_in_node here because the tree-sitter
            # query already finds ALL call nodes including nested ones. The recursive
            # nested call processing was causing O(N*M) complexity where N = number of calls
            # and M = average subtree size, leading to extreme slowdowns on files with
            # many nested calls.

            call_name = self._get_call_target_name(call_node)
            if not call_name:
                continue

            if language == "java" and call_node.type == "method_invocation":
                callee_info = self._resolve_java_method_call(
                    call_node, module_qn, local_var_types
                )
            else:
                callee_info = self._resolve_function_call(
                    call_name, module_qn, local_var_types, class_context
                )
            if not callee_info:
                builtin_info = self._resolve_builtin_call(call_name)
                if not builtin_info:
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
        func_child = call_node.child_by_field_name("function")
        if not func_child:
            return

        if func_child.type == "attribute":
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
        if node.type == "call":
            self._process_nested_calls_in_node(
                node, caller_qn, caller_type, module_qn, local_var_types, class_context
            )

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
                    self.ingestor.ensure_relationship_batch(
                        (caller_type, "qualified_name", caller_qn),
                        "CALLS",
                        (callee_type, "qualified_name", callee_qn),
                    )

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
        if call_name and (
            call_name.startswith("iife_func_") or call_name.startswith("iife_arrow_")
        ):
            iife_qn = f"{module_qn}.{call_name}"
            if iife_qn in self.function_registry:
                return self.function_registry[iife_qn], iife_qn

        if (
            call_name == "super"
            or call_name.startswith("super.")
            or call_name.startswith("super()")
        ):
            return self._resolve_super_call(call_name, module_qn, class_context)

        if "." in call_name and self._is_method_chain(call_name):
            return self._resolve_chained_call(call_name, module_qn, local_var_types)

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]

            if call_name in import_map:
                imported_qn = import_map[call_name]
                if imported_qn in self.function_registry:
                    logger.debug(
                        f"Direct import resolved: {call_name} -> {imported_qn}"
                    )
                    return self.function_registry[imported_qn], imported_qn

            if "." in call_name or "::" in call_name or ":" in call_name:
                if "::" in call_name:
                    separator = "::"
                elif ":" in call_name:
                    separator = ":"
                else:
                    separator = "."
                parts = call_name.split(separator)

                if len(parts) == 2:
                    object_name, method_name = parts

                    if local_var_types and object_name in local_var_types:
                        var_type = local_var_types[object_name]

                        if "." in var_type:
                            class_qn = var_type
                        elif var_type in import_map:
                            class_qn = import_map[var_type]
                        else:
                            class_qn_or_none = self._resolve_class_name(
                                var_type, module_qn
                            )
                            class_qn = class_qn_or_none if class_qn_or_none else ""

                        if class_qn:
                            method_qn = f"{class_qn}{separator}{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    f"Type-inferred object method resolved: "
                                    f"{call_name} -> {method_qn} "
                                    f"(via {object_name}:{var_type})"
                                )
                                return self.function_registry[method_qn], method_qn

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

                        if var_type in self._JS_BUILTIN_TYPES:
                            return (
                                "Function",
                                f"builtin.{var_type}.prototype.{method_name}",
                            )

                    if object_name in import_map:
                        class_qn = import_map[object_name]

                        if "::" in class_qn:
                            rust_parts = class_qn.split("::")
                            class_name = rust_parts[-1]

                            matching_qns = self.function_registry.find_ending_with(
                                class_name
                            )
                            for qn in matching_qns:
                                if self.function_registry.get(qn) == "Class":
                                    class_qn = qn
                                    break

                        potential_class_qn = f"{class_qn}.{object_name}"
                        test_method_qn = f"{potential_class_qn}{separator}{method_name}"
                        if test_method_qn in self.function_registry:
                            class_qn = potential_class_qn

                        registry_separator = separator if separator == ":" else "."
                        method_qn = f"{class_qn}{registry_separator}{method_name}"
                        if method_qn in self.function_registry:
                            logger.debug(
                                f"Import-resolved static call: {call_name} -> {method_qn}"
                            )
                            return self.function_registry[method_qn], method_qn

                    method_qn = f"{module_qn}.{method_name}"
                    if method_qn in self.function_registry:
                        logger.debug(
                            f"Object method resolved: {call_name} -> {method_qn}"
                        )
                        return self.function_registry[method_qn], method_qn

                if len(parts) >= 3 and parts[0] == "self":
                    attribute_ref = ".".join(parts[:-1])
                    method_name = parts[-1]

                    if local_var_types and attribute_ref in local_var_types:
                        var_type = local_var_types[attribute_ref]

                        if "." in var_type:
                            class_qn = var_type
                        elif var_type in import_map:
                            class_qn = import_map[var_type]
                        else:
                            class_qn_or_none = self._resolve_class_name(
                                var_type, module_qn
                            )
                            class_qn = class_qn_or_none if class_qn_or_none else ""

                        if class_qn:
                            method_qn = f"{class_qn}.{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    f"Instance-resolved self-attribute call: "
                                    f"{call_name} -> {method_qn} "
                                    f"(via {attribute_ref}:{var_type})"
                                )
                                return self.function_registry[method_qn], method_qn

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
                    class_name = parts[0]
                    method_name = ".".join(parts[1:])

                    if class_name in import_map:
                        class_qn = import_map[class_name]
                        method_qn = f"{class_qn}.{method_name}"
                        if method_qn in self.function_registry:
                            logger.debug(
                                f"Import-resolved qualified call: "
                                f"{call_name} -> {method_qn}"
                            )
                            return self.function_registry[method_qn], method_qn

                    if local_var_types and class_name in local_var_types:
                        var_type = local_var_types[class_name]

                        if "." in var_type:
                            class_qn = var_type
                        elif var_type in import_map:
                            class_qn = import_map[var_type]
                        else:
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

            for local_name, imported_qn in import_map.items():
                if local_name.startswith("*"):
                    potential_qns = []

                    if "::" in imported_qn:
                        potential_qns.append(f"{imported_qn}::{call_name}")
                    else:
                        potential_qns.append(f"{imported_qn}.{call_name}")
                        potential_qns.append(f"{imported_qn}::{call_name}")

                    for wildcard_qn in potential_qns:
                        if wildcard_qn in self.function_registry:
                            logger.debug(
                                f"Wildcard-resolved call: {call_name} -> {wildcard_qn}"
                            )
                            return self.function_registry[wildcard_qn], wildcard_qn

        same_module_func_qn = f"{module_qn}.{call_name}"
        if same_module_func_qn in self.function_registry:
            logger.debug(
                f"Same-module resolution: {call_name} -> {same_module_func_qn}"
            )
            return (
                self.function_registry[same_module_func_qn],
                same_module_func_qn,
            )

        search_name = re.split(r"[.:]|::", call_name)[-1]

        possible_matches = self.function_registry.find_ending_with(search_name)
        if possible_matches:
            possible_matches.sort(
                key=lambda qn: self._calculate_import_distance(qn, module_qn)
            )
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
        if call_name in self._JS_BUILTIN_PATTERNS:
            return ("Function", f"builtin.{call_name}")

        if (
            call_name.endswith(".bind")
            or call_name.endswith(".call")
            or call_name.endswith(".apply")
        ):
            if call_name.endswith(".bind"):
                return ("Function", "builtin.Function.prototype.bind")
            elif call_name.endswith(".call"):
                return ("Function", "builtin.Function.prototype.call")
            elif call_name.endswith(".apply"):
                return ("Function", "builtin.Function.prototype.apply")

        if ".prototype." in call_name and (
            call_name.endswith(".call") or call_name.endswith(".apply")
        ):
            base_call = call_name.rsplit(".", 1)[0]
            return ("Function", base_call)

        return None

    def _resolve_cpp_operator_call(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        """Resolve C++ operator calls to built-in operator functions."""
        if not call_name.startswith("operator"):
            return None

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

        possible_matches = self.function_registry.find_ending_with(call_name)
        if possible_matches:
            same_module_ops = [
                qn
                for qn in possible_matches
                if qn.startswith(module_qn) and call_name in qn
            ]
            if same_module_ops:
                same_module_ops.sort(key=lambda qn: (len(qn), qn))
                best_candidate = same_module_ops[0]
                return (self.function_registry[best_candidate], best_candidate)

            possible_matches.sort(key=lambda qn: (len(qn), qn))
            best_candidate = possible_matches[0]
            return (self.function_registry[best_candidate], best_candidate)

        return None

    def _is_method_chain(self, call_name: str) -> bool:
        """Check if this appears to be a method chain with parentheses (not just obj.method)."""
        if "(" in call_name and ")" in call_name:
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

        match = re.search(r"\.([^.()]+)$", call_name)
        if not match:
            return None

        final_method = match.group(1)

        object_expr = call_name[: match.start()]

        object_type = self.type_inference._infer_expression_return_type(
            object_expr, module_qn, local_var_types
        )

        if object_type:
            full_object_type = object_type
            if "." not in object_type:
                resolved_class = self._resolve_class_name(object_type, module_qn)
                if resolved_class:
                    full_object_type = resolved_class

            method_qn = f"{full_object_type}.{final_method}"

            if method_qn in self.function_registry:
                logger.debug(
                    f"Resolved chained call: {call_name} -> {method_qn} "
                    f"(via {object_expr}:{object_type})"
                )
                return self.function_registry[method_qn], method_qn

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

        if call_name == "super":
            method_name = "constructor"
        elif call_name.startswith("super."):
            method_name = call_name.split(".", 1)[1]
        elif "." in call_name:
            method_name = call_name.split(".", 1)[1]
        else:
            return None

        current_class_qn = class_context
        if not current_class_qn:
            logger.debug(f"No class context provided for super() call: {call_name}")
            return None

        if current_class_qn not in self.class_inheritance:
            logger.debug(f"No inheritance info for class {current_class_qn}")
            return None

        parent_classes = self.class_inheritance[current_class_qn]
        if not parent_classes:
            logger.debug(f"No parent classes found for {current_class_qn}")
            return None

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
        if class_qn not in self.class_inheritance:
            return None

        queue = list(self.class_inheritance.get(class_qn, []))
        visited = set(queue)

        while queue:
            parent_class_qn = queue.pop(0)
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            if parent_method_qn in self.function_registry:
                return (
                    self.function_registry[parent_method_qn],
                    parent_method_qn,
                )

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

        common_prefix = 0
        for i in range(min(len(caller_parts), len(candidate_parts))):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break

        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

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
                return None

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
        java_engine = self.type_inference.java_type_inference

        result = java_engine.resolve_java_method_call(
            call_node, local_var_types, module_qn
        )

        if result:
            logger.debug(
                f"Java method call resolved: {call_node.text.decode('utf8') if call_node.text else 'unknown'} -> {result[1]}"
            )

        return result
