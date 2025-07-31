"""Definition processor for extracting functions, classes and methods."""

from pathlib import Path
from typing import Any

import toml
from loguru import logger
from tree_sitter import Node, QueryCursor

from ..language_config import LanguageConfig
from ..services.graph_service import MemgraphIngestor
from .import_processor import ImportProcessor
from .utils import resolve_class_name


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
            self._ingest_all_functions(root_node, module_qn, language, queries)
            self._ingest_classes_and_methods(root_node, module_qn, language, queries)

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

            name_node = func_node.child_by_field_name("name")
            if not name_node:
                continue
            text = name_node.text
            if text is None:
                continue
            func_name = text.decode("utf8")

            # Build proper qualified name using existing nested infrastructure
            func_qn = self._build_nested_qualified_name(
                func_node, module_qn, func_name, lang_config
            )
            if func_qn is None:
                func_qn = f"{module_qn}.{func_name}"  # Fallback to simple name

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
            name_node = class_node.child_by_field_name("name")
            if not name_node:
                continue
            text = name_node.text
            if text is None:
                continue
            class_name = text.decode("utf8")
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
            logger.info(f"  Found Class: {class_name} (qn: {class_qn})")
            self.ingestor.ensure_node_batch("Class", class_props)

            # Register the class itself in the function registry
            self.function_registry[class_qn] = "Class"
            self.simple_name_lookup[class_name].add(class_qn)

            # Track inheritance
            parent_classes = self._extract_parent_classes(class_node, module_qn)
            self.class_inheritance[class_qn] = parent_classes

            self.ingestor.ensure_relationship_batch(
                ("Module", "qualified_name", module_qn),
                "DEFINES",
                ("Class", "qualified_name", class_qn),
            )

            # Create INHERITS relationships for each parent class
            for parent_class_qn in parent_classes:
                self.ingestor.ensure_relationship_batch(
                    ("Class", "qualified_name", class_qn),
                    "INHERITS",
                    ("Class", "qualified_name", parent_class_qn),
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
        """Check if method overrides parent class methods & create relationships."""
        if class_qn not in self.class_inheritance:
            return

        # Check each parent class for a method with the same name
        parent_classes = self.class_inheritance[class_qn]
        for parent_class_qn in parent_classes:
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            # Check if parent class has a method with the same name
            if parent_method_qn in self.function_registry:
                self.ingestor.ensure_relationship_batch(
                    ("Method", "qualified_name", method_qn),
                    "OVERRIDES",
                    ("Method", "qualified_name", parent_method_qn),
                )
                logger.debug(
                    f"Method override: {method_qn} OVERRIDES {parent_method_qn}"
                )
                return  # Found direct override, no need to check further

        # If no direct override found, check grandparent classes recursively
        for parent_class_qn in parent_classes:
            self._check_method_overrides_recursive(
                method_qn, method_name, parent_class_qn
            )

    def _check_method_overrides_recursive(
        self, method_qn: str, method_name: str, parent_class_qn: str
    ) -> bool:
        """Recursively check parent classes for method overrides."""
        if parent_class_qn not in self.class_inheritance:
            return False

        grandparent_classes = self.class_inheritance[parent_class_qn]
        for grandparent_qn in grandparent_classes:
            grandparent_method_qn = f"{grandparent_qn}.{method_name}"

            if grandparent_method_qn in self.function_registry:
                self.ingestor.ensure_relationship_batch(
                    ("Method", "qualified_name", method_qn),
                    "OVERRIDES",
                    ("Method", "qualified_name", grandparent_method_qn),
                )
                logger.debug(
                    f"Method override (recursive): {method_qn} OVERRIDES "
                    f"{grandparent_method_qn}"
                )
                return True  # Found override

            # Continue recursively
            if self._check_method_overrides_recursive(
                method_qn, method_name, grandparent_qn
            ):
                return True

        return False

    def _extract_parent_classes(self, class_node: Node, module_qn: str) -> list[str]:
        """Extract parent class names from a class definition."""
        parent_classes = []

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
                                parent_qn = self._resolve_class_name(
                                    parent_name, module_qn
                                )
                                if parent_qn:
                                    parent_classes.append(parent_qn)
                                else:
                                    # Fallback: assume same module
                                    parent_classes.append(f"{module_qn}.{parent_name}")
                        else:
                            # Fallback: assume same module
                            parent_classes.append(f"{module_qn}.{parent_name}")

        return parent_classes

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        """Convert a simple class name to its fully qualified name."""
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )
