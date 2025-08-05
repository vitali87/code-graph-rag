"""Definition processor for extracting functions, classes and methods."""

import textwrap
from collections import deque
from pathlib import Path
from typing import Any

import toml
from loguru import logger
from tree_sitter import Node, Query, QueryCursor

from ..language_config import LanguageConfig
from ..services.graph_service import MemgraphIngestor
from .import_processor import ImportProcessor
from .utils import resolve_class_name

# Common language constants for performance optimization
_JS_TYPESCRIPT_LANGUAGES = {"javascript", "typescript"}


class DefinitionProcessor:
    """Handles processing of function, class, and method definitions."""

    def __init__(
        self,
        ingestor: MemgraphIngestor,
        repo_path: Path,
        project_name: str,
        function_registry: Any,
        simple_name_lookup: dict[str, set[str]],
        import_processor: ImportProcessor,
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.import_processor = import_processor
        self.class_inheritance: dict[str, list[str]] = {}

    def _get_node_type_for_inheritance(self, qualified_name: str) -> str:
        """
        Determine the node type for inheritance relationships.
        Returns the type from the function registry, defaulting to "Class".
        """
        node_type = self.function_registry.get(qualified_name, "Class")
        return str(node_type)

    def _create_inheritance_relationship(
        self, child_node_type: str, child_qn: str, parent_qn: str
    ) -> None:
        """
        Create an INHERITS relationship between child and parent entities.
        Determines the parent type automatically from the function registry.
        """
        parent_type = self._get_node_type_for_inheritance(parent_qn)
        self.ingestor.ensure_relationship_batch(
            (child_node_type, "qualified_name", child_qn),
            "INHERITS",
            (parent_type, "qualified_name", parent_qn),
        )

    def process_file(
        self,
        file_path: Path,
        language: str,
        queries: dict[str, Any],
        structural_elements: dict[Path, str | None],
    ) -> tuple[Node, str] | None:
        """
        Parses a file, ingests its structure and definitions,
        and returns the AST for caching.
        """
        if isinstance(file_path, str):
            file_path = Path(file_path)
        relative_path = file_path.relative_to(self.repo_path)
        relative_path_str = str(relative_path)
        logger.info(f"Parsing and Caching AST for {language}: {relative_path_str}")

        try:
            # Check if language is supported
            if language not in queries:
                logger.warning(f"Unsupported language '{language}' for {file_path}")
                return None

            source_bytes = file_path.read_bytes()
            # We need access to parsers, but we'll get it through queries
            lang_queries = queries[language]
            parser = lang_queries.get("parser")
            if not parser:
                logger.warning(f"No parser available for {language}")
                return None

            tree = parser.parse(source_bytes)
            root_node = tree.root_node

            module_qn = ".".join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name == "__init__.py":
                module_qn = ".".join(
                    [self.project_name] + list(relative_path.parent.parts)
                )

            self.ingestor.ensure_node_batch(
                "Module",
                {
                    "qualified_name": module_qn,
                    "name": file_path.name,
                    "path": relative_path_str,
                },
            )

            # Link Module to its parent Package/Folder
            parent_rel_path = relative_path.parent
            parent_container_qn = structural_elements.get(parent_rel_path)
            parent_label, parent_key, parent_val = (
                ("Package", "qualified_name", parent_container_qn)
                if parent_container_qn
                else (
                    ("Folder", "path", str(parent_rel_path))
                    if parent_rel_path != Path(".")
                    else ("Project", "name", self.project_name)
                )
            )
            self.ingestor.ensure_relationship_batch(
                (parent_label, parent_key, parent_val),
                "CONTAINS_MODULE",
                ("Module", "qualified_name", module_qn),
            )

            self.import_processor.parse_imports(root_node, module_qn, language, queries)
            self._ingest_missing_import_patterns(
                root_node, module_qn, language, queries
            )
            self._ingest_all_functions(root_node, module_qn, language, queries)
            self._ingest_classes_and_methods(root_node, module_qn, language, queries)
            self._ingest_object_literal_methods(root_node, module_qn, language, queries)
            self._ingest_commonjs_exports(root_node, module_qn, language, queries)
            self._ingest_es6_exports(root_node, module_qn, language, queries)
            self._ingest_assignment_arrow_functions(
                root_node, module_qn, language, queries
            )
            self._ingest_prototype_inheritance(root_node, module_qn, language, queries)

            return root_node, language

        except Exception as e:
            logger.error(f"Failed to parse or ingest {file_path}: {e}")
            return None

    def process_dependencies(self, filepath: Path) -> None:
        """Parse pyproject.toml for dependencies."""
        logger.info(f"  Parsing pyproject.toml: {filepath}")
        try:
            data = toml.load(filepath)
            deps = (data.get("tool", {}).get("poetry", {}).get("dependencies", {})) or {
                dep.split(">=")[0].split("==")[0].strip(): dep
                for dep in data.get("project", {}).get("dependencies", [])
            }
            for dep_name, dep_spec in deps.items():
                if dep_name.lower() == "python":
                    continue
                logger.info(f"    Found dependency: {dep_name} (spec: {dep_spec})")
                self.ingestor.ensure_node_batch("ExternalPackage", {"name": dep_name})
                self.ingestor.ensure_relationship_batch(
                    ("Project", "name", self.project_name),
                    "DEPENDS_ON_EXTERNAL",
                    ("ExternalPackage", "name", dep_name),
                    properties={"version_spec": str(dep_spec)},
                )
        except Exception as e:
            logger.error(f"    Error parsing {filepath}: {e}")

    def _get_docstring(self, node: Node) -> str | None:
        """Extracts the docstring from a function or class node's body."""
        body_node = node.child_by_field_name("body")
        if not body_node or not body_node.children:
            return None
        first_statement = body_node.children[0]
        if (
            first_statement.type == "expression_statement"
            and first_statement.children[0].type == "string"
        ):
            text = first_statement.children[0].text
            if text is not None:
                result: str = text.decode("utf-8").strip("'\" \n")
                return result
        return None

    def _extract_decorators(self, node: Node) -> list[str]:
        """Extract decorator names from a decorated node."""
        decorators = []

        # Check if this node has a parent that is a decorated_definition
        current = node.parent
        while current:
            if current.type == "decorated_definition":
                # Get all decorator nodes
                for child in current.children:
                    if child.type == "decorator":
                        decorator_name = self._get_decorator_name(child)
                        if decorator_name:
                            decorators.append(decorator_name)
                break
            current = current.parent

        return decorators

    def _get_decorator_name(self, decorator_node: Node) -> str | None:
        """Extract the name from a decorator node (@decorator or @decorator(...))."""
        # Handle @decorator or @module.decorator
        for child in decorator_node.children:
            if child.type == "identifier":
                text = child.text
                if text is not None:
                    decorator_name: str = text.decode("utf8")
                    return decorator_name
            elif child.type == "attribute":
                # Handle @module.decorator
                text = child.text
                if text is not None:
                    attr_name: str = text.decode("utf8")
                    return attr_name
            elif child.type == "call":
                # Handle @decorator(...) - get the function being called
                func_node = child.child_by_field_name("function")
                if func_node:
                    if func_node.type == "identifier":
                        text = func_node.text
                        if text is not None:
                            func_name: str = text.decode("utf8")
                            return func_name
                    elif func_node.type == "attribute":
                        text = func_node.text
                        if text is not None:
                            func_attr_name: str = text.decode("utf8")
                            return func_attr_name
        return None

    def _extract_template_class_type(self, template_node: Node) -> str | None:
        """Extract the underlying class type from a template declaration."""
        # Look for the class/struct/union specifier within the template
        for child in template_node.children:
            if child.type == "class_specifier":
                return "Class"
            elif child.type == "struct_specifier":
                return "Class"  # In C++, structs are essentially classes
            elif child.type == "union_specifier":
                return "Union"
        return None

    def _extract_cpp_class_name(self, class_node: Node) -> str | None:
        """Extract class name from C++ class/struct/union/enum specifiers."""
        # For template declarations, look inside for the actual class
        if class_node.type == "template_declaration":
            for child in class_node.children:
                if child.type in [
                    "class_specifier",
                    "struct_specifier",
                    "union_specifier",
                ]:
                    return self._extract_cpp_class_name(child)

        # Look for type_identifier (C++ uses this instead of identifier for class names)
        for child in class_node.children:
            if child.type == "type_identifier" and child.text:
                return str(child.text.decode("utf8"))

        # Fallback to regular name field
        name_node = class_node.child_by_field_name("name")
        if name_node and name_node.text:
            return str(name_node.text.decode("utf8"))

        return None

    def _build_cpp_qualified_name(self, node: Node, module_qn: str, name: str) -> str:
        """Build qualified name for C++ entities, handling namespaces properly."""
        path_parts = []
        current = node.parent

        # Walk up the tree to find namespaces
        while current and current.type != "translation_unit":
            if current.type == "namespace_definition":
                # Get namespace name from the 'name' field
                namespace_name = None
                # First try to get the name field directly
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    namespace_name = name_node.text.decode("utf8")
                else:
                    # Fallback: look for namespace_identifier or identifier children
                    for child in current.children:
                        if (
                            child.type in ["namespace_identifier", "identifier"]
                            and child.text
                        ):
                            namespace_name = child.text.decode("utf8")
                            break
                if namespace_name:
                    path_parts.append(namespace_name)
            current = current.parent

        # Reverse to get correct namespace order (outermost first)
        path_parts.reverse()

        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{name}"
        else:
            return f"{module_qn}.{name}"

    def _extract_class_name(self, class_node: Node) -> str | None:
        """Extract class name, handling both class declarations and class expressions."""
        # For regular class declarations, try the name field first
        name_node = class_node.child_by_field_name("name")
        if name_node and name_node.text:
            return str(name_node.text.decode("utf8"))

        # For class expressions, look in parent variable_declarator
        # Pattern: const Animal = class { ... }
        current = class_node.parent
        while current:
            if current.type == "variable_declarator":
                # Find the identifier child (the name)
                for child in current.children:
                    if child.type == "identifier" and child.text:
                        return str(child.text.decode("utf8"))
            current = current.parent

        return None

    def _extract_function_name(self, func_node: Node) -> str | None:
        """Extract function name, handling both regular functions and arrow functions."""
        # For regular functions, try the name field first
        name_node = func_node.child_by_field_name("name")
        if name_node and name_node.text:
            return str(name_node.text.decode("utf8"))

        # For arrow functions, look in parent variable_declarator
        if func_node.type == "arrow_function":
            current = func_node.parent
            while current:
                if current.type == "variable_declarator":
                    # Find the identifier child (the name)
                    for child in current.children:
                        if child.type == "identifier" and child.text:
                            return str(child.text.decode("utf8"))
                current = current.parent

        return None

    def _extract_cpp_function_name(self, func_node: Node) -> str | None:
        """Extract function name from C++ function definitions and declarations."""
        # Handle different C++ function types
        if func_node.type in [
            "function_definition",
            "constructor_or_destructor_definition",
            "inline_method_definition",
            "operator_cast_definition",
        ]:
            # Look for function_declarator within these definitions
            for child in func_node.children:
                if child.type == "function_declarator":
                    name = self._extract_cpp_function_name(child)
                    if name:
                        return name

        elif func_node.type in [
            "field_declaration",
            "declaration",
            "constructor_or_destructor_declaration",
        ]:
            # Handle method declarations - look for function_declarator
            for child in func_node.children:
                if child.type == "function_declarator":
                    name = self._extract_cpp_function_name(child)
                    if name:
                        return name

        elif func_node.type == "function_declarator":
            # Look for identifier, field_identifier, destructor_name, or operator name
            for child in func_node.children:
                if child.type in ["identifier", "field_identifier"] and child.text:
                    return str(child.text.decode("utf8"))
                elif child.type == "operator_name":
                    # Handle operator overloading
                    return self._extract_operator_name(child)
                elif child.type == "destructor_name":
                    # Handle destructor names like ~ClassName
                    return self._extract_destructor_name(child)

        elif func_node.type == "template_declaration":
            # For template functions, look inside the template
            for child in func_node.children:
                if child.type in [
                    "function_definition",
                    "declaration",
                    "constructor_or_destructor_definition",
                ]:
                    return self._extract_cpp_function_name(child)

        elif func_node.type == "lambda_expression":
            # Lambda expressions don't have names, return a synthetic one
            return f"lambda_{func_node.start_point[0] + 1}_{func_node.start_point[1]}"

        # Fallback to regular name field
        name_node = func_node.child_by_field_name("name")
        if name_node and name_node.text:
            return str(name_node.text.decode("utf8"))

        return None

    def _extract_operator_name(self, operator_node: Node) -> str:
        """Extract operator name from operator_name node."""
        # Get the operator text and create a readable name
        if operator_node.text:
            operator_text = operator_node.text.decode("utf8").strip()
            # Convert operators to readable names
            operator_map = {
                "+": "operator_plus",
                "-": "operator_minus",
                "*": "operator_multiply",
                "/": "operator_divide",
                "=": "operator_assign",
                "==": "operator_equal",
                "!=": "operator_not_equal",
                "<": "operator_less",
                ">": "operator_greater",
                "<=": "operator_less_equal",
                ">=": "operator_greater_equal",
                "[]": "operator_subscript",
                "()": "operator_call",
                "++": "operator_increment",
                "--": "operator_decrement",
            }
            return operator_map.get(
                operator_text, f"operator_{operator_text.replace(' ', '_')}"
            )
        return "operator_unknown"

    def _extract_destructor_name(self, destructor_node: Node) -> str:
        """Extract destructor name from destructor_name node."""
        # Destructor name is like ~ClassName, return just the class name
        for child in destructor_node.children:
            if child.type == "identifier" and child.text:
                class_name = child.text.decode("utf8")
                return f"~{class_name}"
        return "~destructor"

    def _generate_anonymous_function_name(self, func_node: Node, module_qn: str) -> str:
        """Generate a synthetic name for anonymous functions (IIFEs, callbacks, etc.)."""
        # Check if this is an IIFE pattern: function -> parenthesized_expression -> call_expression
        parent = func_node.parent
        if parent and parent.type == "parenthesized_expression":
            grandparent = parent.parent
            if grandparent and grandparent.type == "call_expression":
                # Check if the parenthesized expression is the function being called
                if grandparent.child_by_field_name("function") == parent:
                    func_type = (
                        "arrow" if func_node.type == "arrow_function" else "func"
                    )
                    return f"iife_{func_type}_{func_node.start_point[0]}_{func_node.start_point[1]}"

        # Check direct call pattern (less common but possible)
        if parent and parent.type == "call_expression":
            if parent.child_by_field_name("function") == func_node:
                return (
                    f"iife_direct_{func_node.start_point[0]}_{func_node.start_point[1]}"
                )

        # For other anonymous functions (callbacks, etc.), use location-based name
        return f"anonymous_{func_node.start_point[0]}_{func_node.start_point[1]}"

    def _ingest_all_functions(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Extract and ingest all functions (including nested ones)."""
        lang_queries = queries[language]
        lang_config: LanguageConfig = lang_queries["config"]

        query = lang_queries["functions"]
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)

        func_nodes = captures.get("function", [])

        for func_node in func_nodes:
            if not isinstance(func_node, Node):
                logger.warning(
                    f"Expected Node object but got {type(func_node)}: {func_node}"
                )
                continue
            if self._is_method(func_node, lang_config):
                continue

            # Extract function name - use C++ specific logic for C++
            if language == "cpp":
                func_name = self._extract_cpp_function_name(func_node)
                if not func_name:
                    continue  # Skip if we can't extract name for C++ functions
                # Build C++ qualified name with namespace support
                func_qn = self._build_cpp_qualified_name(
                    func_node, module_qn, func_name
                )
            else:
                # Extract function name - handle arrow functions specially
                func_name = self._extract_function_name(func_node)
                if not func_name:
                    # Generate synthetic name for anonymous functions (IIFEs, callbacks, etc.)
                    func_name = self._generate_anonymous_function_name(
                        func_node, module_qn
                    )

                # Build proper qualified name using existing nested infrastructure
                func_qn = (
                    self._build_nested_qualified_name(
                        func_node, module_qn, func_name, lang_config
                    )
                    or f"{module_qn}.{func_name}"
                )  # Fallback to simple name

            # Extract function properties
            decorators = self._extract_decorators(func_node)
            func_props: dict[str, Any] = {
                "qualified_name": func_qn,
                "name": func_name,
                "decorators": decorators,
                "start_line": func_node.start_point[0] + 1,
                "end_line": func_node.end_point[0] + 1,
                "docstring": self._get_docstring(func_node),
            }
            logger.info(f"  Found Function: {func_name} (qn: {func_qn})")
            self.ingestor.ensure_node_batch("Function", func_props)

            self.function_registry[func_qn] = "Function"
            self.simple_name_lookup[func_name].add(func_qn)

            # Determine parent and create proper relationship
            parent_type, parent_qn = self._determine_function_parent(
                func_node, module_qn, lang_config
            )
            self.ingestor.ensure_relationship_batch(
                (parent_type, "qualified_name", parent_qn),
                "DEFINES",
                ("Function", "qualified_name", func_qn),
            )

    def _ingest_top_level_functions(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Extract and ingest top-level functions. (Legacy method, replaced by _ingest_all_functions)"""
        # Keep for backward compatibility, but delegate to new method
        self._ingest_all_functions(root_node, module_qn, language, queries)

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageConfig,
        skip_classes: bool = False,
    ) -> str | None:
        """Build qualified name for nested functions.

        Args:
            skip_classes: If True, skip class nodes in the path (used for object literal methods)
        """
        path_parts = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                f"Unexpected parent type for node {func_node}: {type(current)}. "
                f"Skipping."
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            # Handle functions (named and anonymous)
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode("utf8"))
                else:
                    # Check if this is an anonymous function that has been assigned a name
                    func_name_from_assignment = self._extract_function_name(current)
                    if func_name_from_assignment:
                        path_parts.append(func_name_from_assignment)
            # Handle classes
            elif current.type in lang_config.class_node_types:
                if skip_classes:
                    # For object literal methods, skip the class but continue up the tree
                    pass
                else:
                    # Check if we're inside a method that contains object literals
                    # If so, include the class name in the path. Otherwise, return None (this is a method)
                    if self._is_inside_method_with_object_literals(func_node):
                        if name_node := current.child_by_field_name("name"):
                            text = name_node.text
                            if text is not None:
                                path_parts.append(text.decode("utf8"))
                    else:
                        # Regular class method - return None
                        return None
            # Handle methods inside classes
            elif current.type == "method_definition":
                if name_node := current.child_by_field_name("name"):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode("utf8"))

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

    def _determine_function_parent(
        self, func_node: Node, module_qn: str, lang_config: LanguageConfig
    ) -> tuple[str, str]:
        """Determine the parent of a function (Module or another Function)."""
        current = func_node.parent
        if not isinstance(current, Node):
            return "Module", module_qn

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    parent_text = name_node.text
                    if parent_text is None:
                        continue
                    parent_func_name = parent_text.decode("utf8")
                    if parent_func_qn := self._build_nested_qualified_name(
                        current, module_qn, parent_func_name, lang_config
                    ):
                        return "Function", parent_func_qn
                break

            current = current.parent

        return "Module", module_qn

    def _ingest_classes_and_methods(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Extract and ingest classes and their methods."""
        lang_queries = queries[language]

        query = lang_queries["classes"]
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        class_nodes = captures.get("class", [])

        for class_node in class_nodes:
            if not isinstance(class_node, Node):
                continue
            # Use C++ specific class name extraction for C++ language
            if language == "cpp":
                class_name = self._extract_cpp_class_name(class_node)
                if not class_name:
                    continue
                class_qn = self._build_cpp_qualified_name(
                    class_node, module_qn, class_name
                )
            else:
                class_name = self._extract_class_name(class_node)
                if not class_name:
                    continue
                class_qn = f"{module_qn}.{class_name}"
            decorators = self._extract_decorators(class_node)
            class_props: dict[str, Any] = {
                "qualified_name": class_qn,
                "name": class_name,
                "decorators": decorators,
                "start_line": class_node.start_point[0] + 1,
                "end_line": class_node.end_point[0] + 1,
                "docstring": self._get_docstring(class_node),
            }
            # Determine the correct node type based on the AST node type
            if class_node.type == "interface_declaration":
                node_type = "Interface"
                logger.info(f"  Found Interface: {class_name} (qn: {class_qn})")
            elif class_node.type in [
                "enum_declaration",
                "enum_specifier",
                "enum_class_specifier",
            ]:
                node_type = "Enum"
                logger.info(f"  Found Enum: {class_name} (qn: {class_qn})")
            elif class_node.type == "type_alias_declaration":
                node_type = "Type"
                logger.info(f"  Found Type: {class_name} (qn: {class_qn})")
            elif class_node.type == "struct_specifier":
                node_type = "Class"  # In C++, structs are essentially classes
                logger.info(f"  Found Struct: {class_name} (qn: {class_qn})")
            elif class_node.type == "union_specifier":
                node_type = "Union"
                logger.info(f"  Found Union: {class_name} (qn: {class_qn})")
            elif class_node.type == "template_declaration":
                # For template classes, check the actual class type within
                template_class = self._extract_template_class_type(class_node)
                node_type = template_class if template_class else "Class"
                logger.info(
                    f"  Found Template {node_type}: {class_name} (qn: {class_qn})"
                )
            else:
                node_type = "Class"
                logger.info(f"  Found Class: {class_name} (qn: {class_qn})")

            self.ingestor.ensure_node_batch(node_type, class_props)

            # Register the class/interface/enum itself in the function registry
            self.function_registry[class_qn] = node_type
            self.simple_name_lookup[class_name].add(class_qn)

            # Track inheritance
            parent_classes = self._extract_parent_classes(class_node, module_qn)
            self.class_inheritance[class_qn] = parent_classes

            self.ingestor.ensure_relationship_batch(
                ("Module", "qualified_name", module_qn),
                "DEFINES",
                (node_type, "qualified_name", class_qn),
            )

            # Create INHERITS relationships for each parent class
            for parent_class_qn in parent_classes:
                self._create_inheritance_relationship(
                    node_type, class_qn, parent_class_qn
                )

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

                # Use C++ specific method name extraction for C++
                if language == "cpp":
                    method_name = self._extract_cpp_function_name(method_node)
                    if not method_name:
                        continue
                else:
                    method_name_node = method_node.child_by_field_name("name")
                    if not method_name_node:
                        continue
                    text = method_name_node.text
                    if text is None:
                        continue
                    method_name = text.decode("utf8")

                method_qn = f"{class_qn}.{method_name}"
                decorators = self._extract_decorators(method_node)
                method_props: dict[str, Any] = {
                    "qualified_name": method_qn,
                    "name": method_name,
                    "decorators": decorators,
                    "start_line": method_node.start_point[0] + 1,
                    "end_line": method_node.end_point[0] + 1,
                    "docstring": self._get_docstring(method_node),
                }
                # For C++, create methods as Function nodes to match test expectations
                if language == "cpp":
                    logger.info(f"    Found Method: {method_name} (qn: {method_qn})")
                    self.ingestor.ensure_node_batch("Function", method_props)
                    self.function_registry[method_qn] = "Function"
                else:
                    logger.info(f"    Found Method: {method_name} (qn: {method_qn})")
                    self.ingestor.ensure_node_batch("Method", method_props)
                    self.function_registry[method_qn] = "Method"
                self.simple_name_lookup[method_name].add(method_qn)

                self.ingestor.ensure_relationship_batch(
                    ("Class", "qualified_name", class_qn),
                    "DEFINES_METHOD",
                    ("Method", "qualified_name", method_qn),
                )

                # Note: OVERRIDES relationships will be processed later after all methods are collected

    def process_all_method_overrides(self) -> None:
        """Process OVERRIDES relationships for all methods after collection is complete."""
        logger.info("--- Pass 4: Processing Method Override Relationships ---")

        # Process all methods to find overrides
        for method_qn in self.function_registry.keys():
            if self.function_registry[method_qn] == "Method":
                # Extract class_qn and method_name from method_qn
                if "." in method_qn:
                    parts = method_qn.rsplit(".", 1)
                    if len(parts) == 2:
                        class_qn, method_name = parts
                        self._check_method_overrides(method_qn, method_name, class_qn)

    def _check_method_overrides(
        self, method_qn: str, method_name: str, class_qn: str
    ) -> None:
        """Check if method overrides parent class methods using BFS traversal."""
        if class_qn not in self.class_inheritance:
            return

        # Use BFS to find the nearest parent method in the inheritance hierarchy
        queue = deque([class_qn])
        visited = {class_qn}  # Don't revisit classes (handle diamond inheritance)

        while queue:
            current_class = queue.popleft()

            # Skip the original class (we're looking for parent methods)
            if current_class != class_qn:
                parent_method_qn = f"{current_class}.{method_name}"

                # Check if this parent class has the method
                if parent_method_qn in self.function_registry:
                    self.ingestor.ensure_relationship_batch(
                        ("Method", "qualified_name", method_qn),
                        "OVERRIDES",
                        ("Method", "qualified_name", parent_method_qn),
                    )
                    logger.debug(
                        f"Method override: {method_qn} OVERRIDES {parent_method_qn}"
                    )
                    return  # Found the nearest override, stop searching

            # Add parent classes to queue for next level of BFS
            if current_class in self.class_inheritance:
                for parent_class_qn in self.class_inheritance[current_class]:
                    if parent_class_qn not in visited:
                        visited.add(parent_class_qn)
                        queue.append(parent_class_qn)

    def _extract_parent_classes(self, class_node: Node, module_qn: str) -> list[str]:
        """Extract parent class names from a class definition."""
        parent_classes = []

        # Handle C++ inheritance
        if class_node.type in ["class_specifier", "struct_specifier"]:
            # Look for base_class_clause in C++ class definition
            for child in class_node.children:
                if child.type == "base_class_clause":
                    for base_child in child.children:
                        if base_child.type in [
                            "type_identifier",
                            "qualified_identifier",
                        ]:
                            parent_text = base_child.text
                            if parent_text:
                                parent_name = parent_text.decode("utf8")
                                # For now, assume same module - could be enhanced with proper resolution
                                parent_qn = self._build_cpp_qualified_name(
                                    class_node, module_qn, parent_name.split("::")[-1]
                                )
                                parent_classes.append(parent_qn)
                        elif base_child.type == "access_specifier":
                            # Skip access specifiers like public, private, protected
                            continue
            return parent_classes

        # Look for superclasses in Python class definition
        superclasses_node = class_node.child_by_field_name("superclasses")
        if superclasses_node:
            # Parse the argument_list to get parent classes
            for child in superclasses_node.children:
                if child.type == "identifier":
                    parent_text = child.text
                    if parent_text:
                        parent_name = parent_text.decode("utf8")
                        # Resolve to full qualified name if possible
                        if module_qn in self.import_processor.import_mapping:
                            import_map = self.import_processor.import_mapping[module_qn]
                            if parent_name in import_map:
                                parent_classes.append(import_map[parent_name])
                            else:
                                # Try to resolve within same module
                                resolved_parent = self._resolve_class_name(
                                    parent_name, module_qn
                                )
                                if resolved_parent is not None:
                                    parent_classes.append(resolved_parent)
                                else:
                                    # Fallback: assume same module
                                    parent_classes.append(f"{module_qn}.{parent_name}")
                        else:
                            # Fallback: assume same module
                            parent_classes.append(f"{module_qn}.{parent_name}")

        # Look for inheritance in TypeScript/JavaScript class declaration
        # Structure: class_declaration -> class_heritage -> extends_clause -> identifier
        # Or in JavaScript: class_declaration -> class_heritage -> extends + identifier
        class_heritage_node = None
        for child in class_node.children:
            if child.type == "class_heritage":
                class_heritage_node = child
                break

        if class_heritage_node:
            # TypeScript pattern: class_heritage -> extends_clause -> identifier
            for child in class_heritage_node.children:
                if child.type == "extends_clause":
                    # Find the parent class identifier in the extends_clause
                    for grandchild in child.children:
                        if grandchild.type in ["identifier", "member_expression"]:
                            parent_text = grandchild.text
                            if parent_text:
                                parent_name = parent_text.decode("utf8")
                                parent_classes.append(
                                    self._resolve_js_ts_parent_class(
                                        parent_name, module_qn
                                    )
                                )
                            break
                    break
                # JavaScript pattern: class_heritage -> extends + identifier (direct children)
                elif child.type in ["identifier", "member_expression"]:
                    # Check if the previous sibling is "extends"
                    child_index = class_heritage_node.children.index(child)
                    if (
                        child_index > 0
                        and class_heritage_node.children[child_index - 1].type
                        == "extends"
                    ):
                        parent_text = child.text
                        if parent_text:
                            parent_name = parent_text.decode("utf8")
                            parent_classes.append(
                                self._resolve_js_ts_parent_class(parent_name, module_qn)
                            )
                # Handle mixin patterns: class_heritage -> extends + call_expression
                elif child.type == "call_expression":
                    # Check if the previous sibling is "extends"
                    child_index = class_heritage_node.children.index(child)
                    if (
                        child_index > 0
                        and class_heritage_node.children[child_index - 1].type
                        == "extends"
                    ):
                        # For mixin calls like Swimmable(Animal), extract the base class from arguments
                        parent_classes.extend(
                            self._extract_mixin_parent_classes(child, module_qn)
                        )

        # Look for TypeScript interface inheritance patterns
        # Structure: interface_declaration -> extends_type_clause -> type_identifier
        if class_node.type == "interface_declaration":
            # Look for extends_type_clause (TypeScript interface inheritance)
            extends_type_clause_node = None
            for child in class_node.children:
                if child.type == "extends_type_clause":
                    extends_type_clause_node = child
                    break

            if extends_type_clause_node:
                # Parse interface inheritance from extends_type_clause
                # Pattern: extends_type_clause contains extends + type_identifier(s)
                for child in extends_type_clause_node.children:
                    if child.type == "type_identifier":
                        # Direct type_identifier inheritance
                        parent_text = child.text
                        if parent_text:
                            parent_name = parent_text.decode("utf8")
                            parent_classes.append(
                                self._resolve_js_ts_parent_class(parent_name, module_qn)
                            )

        return parent_classes

    def _extract_mixin_parent_classes(
        self, call_expr_node: Node, module_qn: str
    ) -> list[str]:
        """Extract parent classes from mixin call expressions like Swimmable(Animal)."""
        parent_classes = []

        # Look for arguments in the call expression
        for child in call_expr_node.children:
            if child.type == "arguments":
                # Extract all identifiers from the arguments
                for arg_child in child.children:
                    if arg_child.type == "identifier" and arg_child.text:
                        parent_name = arg_child.text.decode("utf8")
                        parent_classes.append(
                            self._resolve_js_ts_parent_class(parent_name, module_qn)
                        )
                    elif arg_child.type == "call_expression":
                        # Handle nested mixins like Swimmable(Flyable(Animal))
                        parent_classes.extend(
                            self._extract_mixin_parent_classes(arg_child, module_qn)
                        )
                break

        return parent_classes

    def _resolve_js_ts_parent_class(self, parent_name: str, module_qn: str) -> str:
        """Resolve a JavaScript/TypeScript parent class name to its fully qualified name."""
        # Resolve to full qualified name if possible
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if parent_name in import_map:
                return import_map[parent_name]
            else:
                # Try to resolve within same module
                parent_qn = self._resolve_class_name(parent_name, module_qn)
                if parent_qn is not None:
                    return parent_qn
                else:
                    # Fallback: assume same module
                    return f"{module_qn}.{parent_name}"
        else:
            # Fallback: assume same module
            return f"{module_qn}.{parent_name}"

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        """Convert a simple class name to its fully qualified name."""
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _ingest_prototype_inheritance(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect JavaScript prototype inheritance patterns using tree-sitter queries."""
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        # Handle prototype inheritance links
        self._ingest_prototype_inheritance_links(
            root_node, module_qn, language, queries
        )

        # Handle prototype method assignments
        self._ingest_prototype_method_assignments(
            root_node, module_qn, language, queries
        )

    def _ingest_prototype_inheritance_links(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect prototype inheritance links (Child.prototype = Object.create(Parent.prototype))."""
        lang_queries = queries[language]

        # Get the language object for creating queries
        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        # Create a query to find prototype inheritance patterns
        # Pattern: Child.prototype = Object.create(Parent.prototype)
        query_text = """
        (assignment_expression
          left: (member_expression
            object: (identifier) @child_class
            property: (property_identifier) @prototype (#eq? @prototype "prototype"))
          right: (call_expression
            function: (member_expression
              object: (identifier) @object_name (#eq? @object_name "Object")
              property: (property_identifier) @create_method (#eq? @create_method "create"))
            arguments: (arguments
              (member_expression
                object: (identifier) @parent_class
                property: (property_identifier) @parent_prototype (#eq? @parent_prototype "prototype")))))
        """

        try:
            # Create and execute the query for inheritance
            query = Query(language_obj, query_text)
            cursor = QueryCursor(query)
            captures = cursor.captures(root_node)

            # Extract child and parent class names from captures
            child_classes = captures.get("child_class", [])
            parent_classes = captures.get("parent_class", [])

            if child_classes and parent_classes:
                for child_node, parent_node in zip(child_classes, parent_classes):
                    if not child_node.text or not parent_node.text:
                        continue
                    child_name = child_node.text.decode("utf8")
                    parent_name = parent_node.text.decode("utf8")

                    # Build qualified names
                    child_qn = f"{module_qn}.{child_name}"
                    parent_qn = f"{module_qn}.{parent_name}"

                    # Add to inheritance tracking
                    if child_qn not in self.class_inheritance:
                        self.class_inheritance[child_qn] = []
                    if parent_qn not in self.class_inheritance[child_qn]:
                        self.class_inheritance[child_qn].append(parent_qn)

                    # Create inheritance relationship
                    self.ingestor.ensure_relationship_batch(
                        ("Function", "qualified_name", child_qn),
                        "INHERITS",
                        ("Function", "qualified_name", parent_qn),
                    )

                    logger.debug(
                        f"Prototype inheritance: {child_qn} INHERITS {parent_qn}"
                    )

        except Exception as e:
            logger.debug(f"Failed to detect prototype inheritance: {e}")

    def _ingest_prototype_method_assignments(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect prototype method assignments (Constructor.prototype.method = function() {})."""
        lang_queries = queries[language]

        # Get the language object for creating queries
        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        # Detect prototype method assignments: ConstructorFunction.prototype.methodName = function() { ... }
        prototype_method_query = """
        (assignment_expression
          left: (member_expression
            object: (member_expression
              object: (identifier) @constructor_name
              property: (property_identifier) @prototype_keyword (#eq? @prototype_keyword "prototype"))
            property: (property_identifier) @method_name)
          right: (function_expression) @method_function)
        """

        try:
            method_query = Query(language_obj, prototype_method_query)
            method_cursor = QueryCursor(method_query)
            method_captures = method_cursor.captures(root_node)

            constructor_names = method_captures.get("constructor_name", [])
            method_names = method_captures.get("method_name", [])
            method_functions = method_captures.get("method_function", [])

            for constructor_node, method_node, func_node in zip(
                constructor_names, method_names, method_functions
            ):
                constructor_name = (
                    constructor_node.text.decode("utf8")
                    if constructor_node.text
                    else None
                )
                method_name = (
                    method_node.text.decode("utf8") if method_node.text else None
                )

                if constructor_name and method_name:
                    # Create the method as a Function node for prototype methods
                    # Tests expect prototype methods to be in Function nodes
                    constructor_qn = f"{module_qn}.{constructor_name}"
                    method_qn = f"{constructor_qn}.{method_name}"

                    # Create Function node for prototype method
                    method_props = {
                        "qualified_name": method_qn,
                        "name": method_name,
                        "start_line": func_node.start_point[0] + 1,
                        "end_line": func_node.end_point[0] + 1,
                        "docstring": self._get_docstring(func_node),
                    }
                    logger.info(
                        f"  Found Prototype Method: {method_name} (qn: {method_qn})"
                    )
                    self.ingestor.ensure_node_batch("Function", method_props)

                    # Register in function registry as Function
                    self.function_registry[method_qn] = "Function"
                    self.simple_name_lookup[method_name].add(method_qn)

                    # Create relationship from constructor to method
                    self.ingestor.ensure_relationship_batch(
                        ("Function", "qualified_name", constructor_qn),
                        "DEFINES",
                        ("Function", "qualified_name", method_qn),
                    )

                    logger.debug(
                        f"Prototype method: {constructor_qn} DEFINES {method_qn}"
                    )

        except Exception as e:
            logger.debug(f"Failed to detect prototype methods: {e}")

    def _ingest_missing_import_patterns(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect import patterns not handled by the existing import_processor."""
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        lang_queries = queries[language]

        # Get the language object for creating queries
        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        try:
            # Focus only on CommonJS destructuring which import_processor doesn't handle well
            # Handle both shorthand ({ name }) and aliased ({ name: alias }) destructuring
            # More specific query to find only CommonJS destructuring with require
            commonjs_destructure_query = """
            (lexical_declaration
              (variable_declarator
                name: (object_pattern)
                value: (call_expression
                  function: (identifier) @func (#eq? @func "require")
                )
              ) @variable_declarator
            )
            """

            try:
                query = Query(language_obj, commonjs_destructure_query)
                cursor = QueryCursor(query)
                captures = cursor.captures(root_node)

                # Get all variable declarators
                variable_declarators = captures.get("variable_declarator", [])

                # Process each variable declarator separately
                for declarator in variable_declarators:
                    self._process_variable_declarator_for_commonjs(
                        declarator, module_qn
                    )

            except Exception as e:
                logger.debug(f"Failed to process CommonJS destructuring pattern: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect missing import patterns: {e}")

    def _process_variable_declarator_for_commonjs(
        self, declarator: Node, module_qn: str
    ) -> None:
        """Process a single variable declarator to extract CommonJS destructuring imports."""
        try:
            # Check if this is a destructuring assignment with a require call
            # Pattern: const { name1, name2: alias } = require('module')

            # Find the name (left side) - should be an object_pattern for destructuring
            name_node = declarator.child_by_field_name("name")
            if not name_node or name_node.type != "object_pattern":
                return

            # Find the value (right side) - should be a require call
            value_node = declarator.child_by_field_name("value")
            if not value_node or value_node.type != "call_expression":
                return

            # Check if the call is to 'require'
            function_node = value_node.child_by_field_name("function")
            if not function_node or function_node.type != "identifier":
                return

            if (
                function_node.text is None
                or function_node.text.decode("utf8") != "require"
            ):
                return

            # Extract the module name from require arguments
            arguments_node = value_node.child_by_field_name("arguments")
            if not arguments_node or not arguments_node.children:
                return

            # Get the first argument (module name string)
            module_string_node = None
            for child in arguments_node.children:
                if child.type == "string":
                    module_string_node = child
                    break

            if not module_string_node or module_string_node.text is None:
                return

            module_name = module_string_node.text.decode("utf8").strip("'\"")

            # Now extract all destructured variables from the object pattern
            for child in name_node.children:
                if child.type == "shorthand_property_identifier_pattern":
                    # Handle shorthand destructuring: { name }
                    if child.text is not None:
                        destructured_name = child.text.decode("utf8")
                        self._process_commonjs_import(
                            destructured_name, module_name, module_qn
                        )

                elif child.type == "pair_pattern":
                    # Handle aliased destructuring: { name: alias }
                    key_node = child.child_by_field_name("key")
                    value_node = child.child_by_field_name("value")

                    if (
                        key_node
                        and key_node.type == "property_identifier"
                        and value_node
                        and value_node.type == "identifier"
                    ):
                        if value_node.text is not None:
                            alias_name = value_node.text.decode("utf8")
                            self._process_commonjs_import(
                                alias_name, module_name, module_qn
                            )

        except Exception as e:
            logger.debug(f"Failed to process variable declarator for CommonJS: {e}")

    def _process_commonjs_import(
        self, imported_name: str, module_name: str, module_qn: str
    ) -> None:
        """Process a single CommonJS import (either shorthand or aliased)."""
        try:
            # Use the existing import_processor's path resolution
            resolved_source_module = self.import_processor._resolve_js_module_path(
                module_name, module_qn
            )

            # Check if this import relationship already exists to avoid duplicates
            import_key = f"{module_qn}->{resolved_source_module}"
            if import_key not in getattr(self, "_processed_imports", set()):
                # Create the source module node if it doesn't exist
                self.ingestor.ensure_node_batch(
                    "Module",
                    {
                        "qualified_name": resolved_source_module,
                        "name": resolved_source_module,
                    },
                )

                # Create the relationship
                self.ingestor.ensure_relationship_batch(
                    ("Module", "qualified_name", module_qn),
                    "IMPORTS",
                    (
                        "Module",
                        "qualified_name",
                        resolved_source_module,
                    ),
                )

                logger.debug(
                    f"Missing pattern: {module_qn} IMPORTS {imported_name} from {resolved_source_module}"
                )

                # Track processed imports to avoid duplicates
                if not hasattr(self, "_processed_imports"):
                    self._processed_imports = set()
                self._processed_imports.add(import_key)

        except Exception as e:
            logger.debug(f"Failed to process CommonJS import {imported_name}: {e}")

    def _ingest_object_literal_methods(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect and ingest methods defined in object literals."""
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        lang_queries = queries[language]

        # Get the language object for creating queries
        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        try:
            # Query for object literal methods (pair with function_expression)
            object_method_query = """
            (pair
              key: (property_identifier) @method_name
              value: (function_expression) @method_function)
            """

            # Query for method definitions in object literals inside functions
            # Only matches methods that are truly nested within function contexts
            method_def_query = """
            (object
              (method_definition
                name: (property_identifier) @method_name) @method_function)
            """

            # Process both patterns
            for query_text in [object_method_query, method_def_query]:
                try:
                    query = Query(language_obj, query_text)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    method_names = captures.get("method_name", [])
                    method_functions = captures.get("method_function", [])

                    for method_name_node, method_func_node in zip(
                        method_names, method_functions
                    ):
                        if method_name_node.text and method_func_node:
                            method_name = method_name_node.text.decode("utf8")

                            # Skip if this is a regular class method (not object literal inside a method)
                            if self._is_class_method(
                                method_func_node
                            ) and not self._is_inside_method_with_object_literals(
                                method_func_node
                            ):
                                continue

                            # Get language config for nested name building
                            lang_config = lang_queries.get("config")
                            if lang_config:
                                # Build proper nested qualified name for object method
                                method_qn = self._build_object_method_qualified_name(
                                    method_name_node,
                                    method_func_node,
                                    module_qn,
                                    method_name,
                                    lang_config,
                                )
                                if method_qn is None:
                                    method_qn = f"{module_qn}.{method_name}"
                            else:
                                # Fallback to old logic if no config
                                object_name = self._find_object_name_for_method(
                                    method_name_node
                                )
                                if object_name:
                                    method_qn = (
                                        f"{module_qn}.{object_name}.{method_name}"
                                    )
                                else:
                                    method_qn = f"{module_qn}.{method_name}"

                            # Create Function node for object literal method
                            method_props = {
                                "qualified_name": method_qn,
                                "name": method_name,
                                "start_line": method_func_node.start_point[0] + 1,
                                "end_line": method_func_node.end_point[0] + 1,
                                "docstring": self._get_docstring(method_func_node),
                            }
                            logger.info(
                                f"  Found Object Method: {method_name} (qn: {method_qn})"
                            )
                            self.ingestor.ensure_node_batch("Function", method_props)

                            # Register in function registry
                            self.function_registry[method_qn] = "Function"
                            self.simple_name_lookup[method_name].add(method_qn)

                            # Create relationship from module to method
                            self.ingestor.ensure_relationship_batch(
                                ("Module", "qualified_name", module_qn),
                                "DEFINES",
                                ("Function", "qualified_name", method_qn),
                            )

                except Exception as e:
                    logger.debug(f"Failed to process object literal methods: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect object literal methods: {e}")

    def _ingest_commonjs_exports(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect and ingest CommonJS exports as function definitions."""
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        lang_queries = queries[language]
        language_obj = lang_queries.get("language")
        if not language_obj:
            return

        try:
            # Query for exports.name = function patterns
            exports_function_query = """
            (assignment_expression
              left: (member_expression
                object: (identifier) @exports_obj
                property: (property_identifier) @export_name)
              right: [(function_expression) (arrow_function)] @export_function)
            """

            # Query for module.exports.name = function patterns
            module_exports_query = """
            (assignment_expression
              left: (member_expression
                object: (member_expression
                  object: (identifier) @module_obj
                  property: (property_identifier) @exports_prop)
                property: (property_identifier) @export_name)
              right: [(function_expression) (arrow_function)] @export_function)
            """

            for query_text in [exports_function_query, module_exports_query]:
                try:
                    query = Query(language_obj, query_text)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    exports_objs = captures.get("exports_obj", [])
                    module_objs = captures.get("module_obj", [])
                    exports_props = captures.get("exports_prop", [])
                    export_names = captures.get("export_name", [])
                    export_functions = captures.get("export_function", [])

                    # Process exports.name = function patterns
                    for i, (exports_obj, export_name, export_function) in enumerate(
                        zip(exports_objs, export_names, export_functions)
                    ):
                        if (
                            exports_obj.text
                            and export_name.text
                            and exports_obj.text.decode("utf8") == "exports"
                        ):
                            function_name = export_name.text.decode("utf8")

                            # Skip if this export is inside a function (let regular processing handle it)
                            if self._is_export_inside_function(export_function):
                                continue

                            function_qn = f"{module_qn}.{function_name}"

                            function_props = {
                                "qualified_name": function_qn,
                                "name": function_name,
                                "start_line": export_function.start_point[0] + 1,
                                "end_line": export_function.end_point[0] + 1,
                                "docstring": self._get_docstring(export_function),
                            }

                            logger.info(
                                f"  Found CommonJS Export: {function_name} (qn: {function_qn})"
                            )
                            self.ingestor.ensure_node_batch("Function", function_props)
                            self.function_registry[function_qn] = "Function"
                            self.simple_name_lookup[function_name].add(function_qn)

                    # Process module.exports.name = function patterns
                    for i, (
                        module_obj,
                        exports_prop,
                        export_name,
                        export_function,
                    ) in enumerate(
                        zip(module_objs, exports_props, export_names, export_functions)
                    ):
                        if (
                            module_obj.text
                            and exports_prop.text
                            and export_name.text
                            and module_obj.text.decode("utf8") == "module"
                            and exports_prop.text.decode("utf8") == "exports"
                        ):
                            function_name = export_name.text.decode("utf8")

                            # Skip if this export is inside a function (let regular processing handle it)
                            if self._is_export_inside_function(export_function):
                                continue

                            function_qn = f"{module_qn}.{function_name}"

                            function_props = {
                                "qualified_name": function_qn,
                                "name": function_name,
                                "start_line": export_function.start_point[0] + 1,
                                "end_line": export_function.end_point[0] + 1,
                                "docstring": self._get_docstring(export_function),
                            }

                            logger.info(
                                f"  Found CommonJS Module Export: {function_name} (qn: {function_qn})"
                            )
                            self.ingestor.ensure_node_batch("Function", function_props)
                            self.function_registry[function_qn] = "Function"
                            self.simple_name_lookup[function_name].add(function_qn)

                except Exception as e:
                    logger.debug(f"Failed to process CommonJS exports query: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect CommonJS exports: {e}")

    def _ingest_es6_exports(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect and ingest ES6 export statements as function definitions."""
        try:
            lang_query = queries[language]["language"]

            # Query for export const name = function patterns
            export_const_query = """
            (export_statement
              (lexical_declaration
                (variable_declarator
                  name: (identifier) @export_name
                  value: [(function_expression) (arrow_function)] @export_function)))
            """

            # Query for export function name patterns
            export_function_query = """
            (export_statement
              [(function_declaration) (generator_function_declaration)] @export_function)
            """

            for query_text in [export_const_query, export_function_query]:
                try:
                    cleaned_query = textwrap.dedent(query_text).strip()
                    query = Query(lang_query, cleaned_query)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    export_names = captures.get("export_name", [])
                    export_functions = captures.get("export_function", [])

                    # Process export const name = function patterns
                    for i, (export_name, export_function) in enumerate(
                        zip(export_names, export_functions)
                    ):
                        if export_name.text and export_function:
                            function_name = export_name.text.decode("utf8")

                            # Skip if this export is inside a function (let regular processing handle it)
                            if self._is_export_inside_function(export_function):
                                continue

                            function_qn = f"{module_qn}.{function_name}"

                            function_props = {
                                "qualified_name": function_qn,
                                "name": function_name,
                                "start_line": export_function.start_point[0] + 1,
                                "end_line": export_function.end_point[0] + 1,
                                "docstring": self._get_docstring(export_function),
                            }

                            logger.debug(
                                f"  Found ES6 Export Function: {function_name} (qn: {function_qn})"
                            )
                            self.ingestor.ensure_node_batch("Function", function_props)
                            self.function_registry[function_qn] = "Function"
                            self.simple_name_lookup[function_name].add(function_qn)

                    # Process export function patterns (function declarations)
                    if not export_names:  # Only function declarations
                        for export_function in export_functions:
                            if export_function:
                                # Get function name from the function declaration
                                function_name = None
                                if name_node := export_function.child_by_field_name(
                                    "name"
                                ):
                                    if name_node.text:
                                        function_name = name_node.text.decode("utf8")

                                if function_name:
                                    # Skip if this export is inside a function (let regular processing handle it)
                                    if self._is_export_inside_function(export_function):
                                        continue

                                    function_qn = f"{module_qn}.{function_name}"

                                    function_props = {
                                        "qualified_name": function_qn,
                                        "name": function_name,
                                        "start_line": export_function.start_point[0]
                                        + 1,
                                        "end_line": export_function.end_point[0] + 1,
                                        "docstring": self._get_docstring(
                                            export_function
                                        ),
                                    }

                                    logger.debug(
                                        f"  Found ES6 Export Function Declaration: {function_name} (qn: {function_qn})"
                                    )
                                    self.ingestor.ensure_node_batch(
                                        "Function", function_props
                                    )
                                    self.function_registry[function_qn] = "Function"
                                    self.simple_name_lookup[function_name].add(
                                        function_qn
                                    )

                except Exception as e:
                    logger.debug(f"Failed to process ES6 exports query: {e}")

        except Exception as e:
            logger.debug(f"Failed to detect ES6 exports: {e}")

    def _ingest_assignment_arrow_functions(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Detect arrow functions in assignment expressions and object literals."""
        # Only apply to JavaScript/TypeScript
        if language not in _JS_TYPESCRIPT_LANGUAGES:
            return

        try:
            lang_query = queries[language]["language"]

            # Query for object literal arrow functions: { arrowMethod: () => {} }
            object_arrow_query = """
            (object
              (pair
                (property_identifier) @method_name
                (arrow_function) @arrow_function))
            """

            # Query for assignment arrow functions: this.arrowProperty = () => {}
            assignment_arrow_query = """
            (assignment_expression
              (member_expression) @member_expr
              (arrow_function) @arrow_function)
            """

            # Query for assignment function expressions: this.funcProperty = function() {}
            assignment_function_query = """
            (assignment_expression
              (member_expression) @member_expr
              (function_expression) @function_expr)
            """

            for query_text in [
                object_arrow_query,
                assignment_arrow_query,
                assignment_function_query,
            ]:
                try:
                    query = Query(lang_query, query_text)
                    cursor = QueryCursor(query)
                    captures = cursor.captures(root_node)

                    method_names = captures.get("method_name", [])
                    member_exprs = captures.get("member_expr", [])
                    arrow_functions = captures.get("arrow_function", [])
                    function_exprs = captures.get("function_expr", [])

                    # Process object literal arrow methods
                    for method_name, arrow_function in zip(
                        method_names, arrow_functions
                    ):
                        if method_name.text and arrow_function:
                            function_name = method_name.text.decode("utf8")

                            # Get language config for nested name building
                            lang_config = queries[language].get("config")
                            if lang_config:
                                # Use the same nested qualified name logic as functions
                                function_qn = self._build_nested_qualified_name(
                                    arrow_function,
                                    module_qn,
                                    function_name,
                                    lang_config,
                                    skip_classes=False,
                                )
                                if function_qn is None:
                                    function_qn = f"{module_qn}.{function_name}"
                            else:
                                function_qn = f"{module_qn}.{function_name}"

                            function_props = {
                                "qualified_name": function_qn,
                                "name": function_name,
                                "start_line": arrow_function.start_point[0] + 1,
                                "end_line": arrow_function.end_point[0] + 1,
                                "docstring": self._get_docstring(arrow_function),
                            }

                            logger.debug(
                                f"  Found Object Arrow Function: {function_name} (qn: {function_qn})"
                            )
                            self.ingestor.ensure_node_batch("Function", function_props)
                            self.function_registry[function_qn] = "Function"
                            self.simple_name_lookup[function_name].add(function_qn)

                    # Process assignment arrow functions
                    for member_expr, arrow_function in zip(
                        member_exprs, arrow_functions
                    ):
                        if member_expr.text and arrow_function:
                            # Extract property name from this.propertyName
                            member_text = member_expr.text.decode("utf8")
                            if "." in member_text:
                                function_name = member_text.split(".")[
                                    -1
                                ]  # Get the property name

                                # Get language config for nested name building
                                lang_config = queries[language].get("config")
                                if lang_config:
                                    # Use specialized logic for assignment arrow functions
                                    function_qn = self._build_assignment_arrow_function_qualified_name(
                                        member_expr,
                                        arrow_function,
                                        module_qn,
                                        function_name,
                                        lang_config,
                                    )
                                    if function_qn is None:
                                        function_qn = f"{module_qn}.{function_name}"
                                else:
                                    function_qn = f"{module_qn}.{function_name}"

                                function_props = {
                                    "qualified_name": function_qn,
                                    "name": function_name,
                                    "start_line": arrow_function.start_point[0] + 1,
                                    "end_line": arrow_function.end_point[0] + 1,
                                    "docstring": self._get_docstring(arrow_function),
                                }

                                logger.debug(
                                    f"  Found Assignment Arrow Function: {function_name} (qn: {function_qn})"
                                )
                                self.ingestor.ensure_node_batch(
                                    "Function", function_props
                                )
                                self.function_registry[function_qn] = "Function"
                                self.simple_name_lookup[function_name].add(function_qn)

                    # Process assignment function expressions
                    for member_expr, function_expr in zip(member_exprs, function_exprs):
                        if member_expr.text and function_expr:
                            # Extract property name from this.propertyName
                            member_text = member_expr.text.decode("utf8")
                            if "." in member_text:
                                function_name = member_text.split(".")[
                                    -1
                                ]  # Get the property name

                                # Get language config for nested name building
                                lang_config = queries[language].get("config")
                                if lang_config:
                                    # Use specialized logic for assignment function expressions
                                    function_qn = self._build_assignment_arrow_function_qualified_name(
                                        member_expr,
                                        function_expr,
                                        module_qn,
                                        function_name,
                                        lang_config,
                                    )
                                    if function_qn is None:
                                        function_qn = f"{module_qn}.{function_name}"
                                else:
                                    function_qn = f"{module_qn}.{function_name}"

                                function_props = {
                                    "qualified_name": function_qn,
                                    "name": function_name,
                                    "start_line": function_expr.start_point[0] + 1,
                                    "end_line": function_expr.end_point[0] + 1,
                                    "docstring": self._get_docstring(function_expr),
                                }

                                logger.debug(
                                    f"  Found Assignment Function Expression: {function_name} (qn: {function_qn})"
                                )
                                self.ingestor.ensure_node_batch(
                                    "Function", function_props
                                )
                                self.function_registry[function_qn] = "Function"
                                self.simple_name_lookup[function_name].add(function_qn)

                except Exception as e:
                    logger.debug(
                        f"Failed to process assignment arrow functions query: {e}"
                    )

        except Exception as e:
            logger.debug(f"Failed to detect assignment arrow functions: {e}")

    def _is_static_method_in_class(self, method_node: Node) -> bool:
        """Check if this method is a static method inside a class definition."""
        # Check if method has static keyword as sibling
        if method_node.type == "method_definition":
            # Check if any sibling or parent has "static" keyword
            parent = method_node.parent
            if parent and parent.type == "class_body":
                # Look for static keyword in the method definition
                for child in method_node.children:
                    if child.type == "static":
                        return True
        return False

    def _is_method_in_class(self, method_node: Node) -> bool:
        """Check if this method is inside a class definition (static or instance)."""
        # Walk up the tree to see if we're inside a class
        current = method_node.parent
        while current:
            if current.type == "class_body":
                return True
            current = current.parent
        return False

    def _is_inside_method_with_object_literals(self, func_node: Node) -> bool:
        """Check if this function is an object literal method inside a class method."""
        # Walk up to see if we're inside an object literal inside a method_definition
        current = func_node.parent
        found_object = False

        while current:
            if current.type == "object":
                found_object = True
            elif current.type == "method_definition" and found_object:
                # We're inside an object literal inside a method - this should be nested
                return True
            elif current.type == "class_body":
                # Reached class body - stop looking
                break
            current = current.parent

        return False

    def _is_class_method(self, method_node: Node) -> bool:
        """Check if a method definition is inside a class body."""
        current = method_node.parent
        while current:
            if current.type == "class_body":
                return True
            elif current.type in ["program", "module"]:
                return False
            current = current.parent
        return False

    def _is_export_inside_function(self, export_node: Node) -> bool:
        """Check if this export statement is inside a function body."""
        current = export_node.parent
        while current:
            if current.type in [
                "function_declaration",
                "function_expression",
                "arrow_function",
                "method_definition",
            ]:
                return True
            elif current.type in ["program", "module"]:
                # Reached module level - not inside a function
                return False
            current = current.parent
        return False

    def _find_object_name_for_method(self, method_name_node: Node) -> str | None:
        """Find the object variable name that contains this method, using proper tree-sitter traversal."""
        # Walk up the tree to find the variable declarator or assignment
        current = method_name_node.parent
        while current:
            if current.type == "variable_declarator":
                # Use field-based access to get the variable name
                name_node = current.child_by_field_name("name")
                if name_node and name_node.type == "identifier" and name_node.text:
                    return str(name_node.text.decode("utf8"))
            elif current.type == "assignment_expression":
                # Use field-based access to get assignment target
                left_child = current.child_by_field_name("left")
                if left_child and left_child.type == "identifier" and left_child.text:
                    return str(left_child.text.decode("utf8"))
            current = current.parent
        return None

    def _build_object_method_qualified_name(
        self,
        method_name_node: Node,
        method_func_node: Node,
        module_qn: str,
        method_name: str,
        lang_config: LanguageConfig,
    ) -> str | None:
        """Build proper qualified name for object literal methods using tree-sitter traversal.

        Skips intermediate object variable names to get semantic nesting like:
        - createApiClient.get (not createApiClient.client.get)
        - ServiceFactory.createService.process (not ServiceFactory.createService.{obj}.process)
        """
        path_parts = []

        # Start from the method name node and walk up to find the containing function/class context
        current = method_name_node.parent

        # Walk up past the object literal and variable declaration to find the containing function
        while current and current.type not in lang_config.module_node_types:
            # Skip these object-related nodes to get to the containing function
            if current.type in [
                "object",
                "variable_declarator",
                "variable_declaration",
                "assignment_expression",
                "pair",
            ]:
                current = current.parent
                continue

            # Handle functions (named and anonymous)
            if current.type in lang_config.function_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    path_parts.append(name_node.text.decode("utf8"))
            # Handle classes
            elif current.type in lang_config.class_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    path_parts.append(name_node.text.decode("utf8"))
            # Handle methods inside classes
            elif current.type == "method_definition":
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    path_parts.append(name_node.text.decode("utf8"))

            current = current.parent

        # Reverse the path parts to get correct order (module -> class -> method -> method)
        path_parts.reverse()

        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{method_name}"
        else:
            return f"{module_qn}.{method_name}"

    def _build_assignment_arrow_function_qualified_name(
        self,
        member_expr: Node,
        arrow_function: Node,
        module_qn: str,
        function_name: str,
        lang_config: LanguageConfig,
    ) -> str | None:
        """Build proper qualified name for arrow functions in assignments using tree-sitter traversal.

        Handles cases like:
        - this.fetchUser = () => {} in constructor
        - this.retry = () => {} in method
        """
        path_parts = []

        # Start from the assignment expression and walk up to find the containing context
        current = member_expr.parent  # assignment_expression
        if current and current.type == "assignment_expression":
            current = current.parent  # expression_statement or other container

        # Walk up to find containing functions/classes/methods
        while current and current.type not in lang_config.module_node_types:
            # Skip expression statements and other non-semantic nodes
            if current.type in ["expression_statement", "statement_block"]:
                current = current.parent
                continue

            # Handle functions (named and anonymous)
            if current.type in lang_config.function_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    path_parts.append(name_node.text.decode("utf8"))
            # Handle classes
            elif current.type in lang_config.class_node_types:
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    path_parts.append(name_node.text.decode("utf8"))
            # Handle methods inside classes (constructor, initialize, etc.)
            elif current.type == "method_definition":
                name_node = current.child_by_field_name("name")
                if name_node and name_node.text:
                    path_parts.append(name_node.text.decode("utf8"))

            current = current.parent

        # Reverse the path parts to get correct order (module -> class -> method -> function)
        path_parts.reverse()

        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{function_name}"
        else:
            return f"{module_qn}.{function_name}"
