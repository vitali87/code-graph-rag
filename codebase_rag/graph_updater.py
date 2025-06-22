import os
import platform
import toml
from pathlib import Path
from typing import Any, Optional, Dict

import mgclient
from loguru import logger
from tree_sitter import Language, Parser, Node
from tree_sitter_python import (
    language as python_language_so,
)

from .language_config import get_language_config, LanguageConfig


class MemgraphIngestor:
    """Handles all communication and query execution with the Memgraph database."""

    def __init__(self, host: str, port: int, batch_size: int = 1000):
        self._host = host
        self._port = port
        self.batch_size = batch_size
        self.conn: Optional[mgclient.Connection] = None
        self.node_buffer: list[tuple[str, dict[str, Any]]] = []
        self.relationship_buffer: list[tuple[tuple, str, tuple, Optional[dict]]] = []

    def __enter__(self):
        logger.info(f"Connecting to Memgraph at {self._host}:{self._port}...")
        self.conn = mgclient.connect(host=self._host, port=self._port)
        self.conn.autocommit = True
        logger.info("Successfully connected to Memgraph.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            logger.error(
                f"An exception occurred: {exc_val}. Flushing remaining items...",
                exc_info=True,
            )
        self.flush_all()
        if self.conn:
            self.conn.close()
            logger.info("\nDisconnected from Memgraph.")

    def _execute_query(
        self, query: str, params: Optional[dict[str, Any]] = None
    ) -> list:
        if not self.conn:
            raise ConnectionError("Not connected to Memgraph.")
        params = params or {}
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            if not cursor.description:
                return []
            column_names = [desc.name for desc in cursor.description]
            return [dict(zip(column_names, row)) for row in cursor.fetchall()]
        except Exception as e:
            if (
                "already exists" not in str(e).lower()
                and "constraint" not in str(e).lower()
            ):
                logger.error(f"!!! Cypher Error: {e}")
                logger.error(f"    Query: {query}")
                logger.error(f"    Params: {params}")
            raise
        finally:
            if cursor:
                cursor.close()

    def _execute_batch(self, query: str, params_list: list[dict[str, Any]]) -> None:
        if not self.conn or not params_list:
            return
        cursor = None
        try:
            cursor = self.conn.cursor()
            batch_query = f"UNWIND $batch AS row\n{query}"
            cursor.execute(batch_query, {"batch": params_list})
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.error(f"!!! Batch Cypher Error: {e}")
        finally:
            if cursor:
                cursor.close()

    def clean_database(self) -> None:
        logger.info("--- Cleaning database... ---")
        self._execute_query("MATCH (n) DETACH DELETE n;")
        logger.info("--- Database cleaned. ---")

    def ensure_constraints(self) -> None:
        logger.info("Ensuring constraints...")
        constraints = {
            "Project": "name",
            "Package": "qualified_name",
            "Folder": "path",
            "Module": "qualified_name",
            "Class": "qualified_name",
            "Function": "qualified_name",
            "Method": "qualified_name",
            "File": "path",
            "ExternalPackage": "name",
        }
        for label, prop in constraints.items():
            try:
                self._execute_query(
                    f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"
                )
            except:  # Fails if constraint exists, which is fine
                pass
        logger.info("Constraints checked/created.")

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
        self.node_buffer.append((label, properties))
        if len(self.node_buffer) >= self.batch_size:
            self.flush_nodes()

    def ensure_relationship_batch(
        self,
        from_node: tuple,
        rel_type: str,
        to_node: tuple,
        properties: Optional[dict[str, Any]] = None,
    ) -> None:
        self.relationship_buffer.append((from_node, rel_type, to_node, properties))
        if len(self.relationship_buffer) >= self.batch_size:
            self.flush_relationships()

    def flush_nodes(self) -> None:
        if not self.node_buffer:
            return
        from collections import defaultdict

        nodes_by_label = defaultdict(list)
        for label, props in self.node_buffer:
            nodes_by_label[label].append(props)
        for label, props_list in nodes_by_label.items():
            if not props_list:
                continue
            id_key = next(iter(props_list[0]))
            prop_keys = list(props_list[0].keys())
            set_clause = ", ".join([f"n.{key} = row.{key}" for key in prop_keys])
            query = (
                f"MERGE (n:{label} {{{id_key}: row.{id_key}}}) "
                f"ON CREATE SET {set_clause} ON MATCH SET {set_clause}"
            )
            self._execute_batch(query, props_list)
        logger.info(f"Flushed {len(self.node_buffer)} nodes.")
        self.node_buffer.clear()

    def flush_relationships(self) -> None:
        if not self.relationship_buffer:
            return
        from collections import defaultdict

        rels_by_pattern = defaultdict(list)
        for from_node, rel_type, to_node, props in self.relationship_buffer:
            pattern = (from_node[0], from_node[1], rel_type, to_node[0], to_node[1])
            rels_by_pattern[pattern].append(
                {"from_val": from_node[2], "to_val": to_node[2], "props": props or {}}
            )
        for pattern, params_list in rels_by_pattern.items():
            from_label, from_key, rel_type, to_label, to_key = pattern
            query = (
                f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
                f"(b:{to_label} {{{to_key}: row.to_val}})\n"
                f"MERGE (a)-[r:{rel_type}]->(b)"
            )
            if any(p["props"] for p in params_list):
                query += "\nSET r += row.props"
            self._execute_batch(query, params_list)
        logger.info(f"Flushed {len(self.relationship_buffer)} relationships.")
        self.relationship_buffer.clear()

    def flush_all(self) -> None:
        logger.info("--- Flushing all pending writes to database... ---")
        self.flush_nodes()
        self.flush_relationships()
        logger.info("--- Flushing complete. ---")


class GraphUpdater:
    """Parses code using Tree-sitter and updates the graph."""

    def __init__(self, ingestor: MemgraphIngestor, repo_path: Path):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = repo_path.name
        self.structural_elements: Dict[Path, Optional[str]] = {}
        self.ignore_dirs = {
            ".git",
            "venv",
            ".venv",
            "__pycache__",
            "node_modules",
            "build",
            "dist",
            ".eggs",
        }

        self.parser = Parser()
        self.language = Language(python_language_so())
        self.parser.language = self.language
        logger.success("Successfully loaded Python grammar.")

        self._compile_queries()

    def _compile_queries(self):
        """Compiles all Tree-sitter queries for efficiency based on language config."""
        # For now, using Python config - will be configurable later
        from .language_config import LANGUAGE_CONFIGS

        self.lang_config = LANGUAGE_CONFIGS["python"]

        # Build queries dynamically based on language configuration
        function_patterns = " ".join(
            [
                f"({node_type}) @function"
                for node_type in self.lang_config.function_node_types
            ]
        )
        class_patterns = " ".join(
            [f"({node_type}) @class" for node_type in self.lang_config.class_node_types]
        )

        self.queries = {
            "functions": self.language.query(function_patterns),
            "classes": self.language.query(class_patterns),
        }

    def run(self) -> None:
        """Orchestrates the two-pass parsing and ingestion process."""
        self.ingestor.ensure_node_batch("Project", {"name": self.project_name})
        logger.info(f"Ensuring Project: {self.project_name}")

        logger.info("--- Pass 1: Identifying Packages and Folders ---")
        self._identify_structure()

        logger.info("\n--- Pass 2: Processing Files and Python Modules ---")
        self._process_files()

        logger.info("\n--- Analysis complete. Flushing all data to database... ---")
        self.ingestor.flush_all()

    def _identify_structure(self) -> None:
        """First pass: Walks the directory to find all packages and folders."""
        for root_str, dirs, _ in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)

            parent_rel_path = relative_root.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)

            if (root / "__init__.py").exists():
                package_qn = ".".join([self.project_name] + list(relative_root.parts))
                self.structural_elements[relative_root] = package_qn
                logger.info(f"  Identified Package: {package_qn}")
                self.ingestor.ensure_node_batch(
                    "Package",
                    {
                        "qualified_name": package_qn,
                        "name": root.name,
                        "path": str(relative_root),
                    },
                )
                parent_label, parent_key, parent_val = (
                    ("Project", "name", self.project_name)
                    if parent_rel_path == Path(".")
                    else ("Package", "qualified_name", parent_container_qn)
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_label, parent_key, parent_val),
                    "CONTAINS_PACKAGE",
                    ("Package", "qualified_name", package_qn),
                )
            elif root != self.repo_path:
                self.structural_elements[relative_root] = None  # Mark as folder
                logger.info(f"  Identified Folder: '{relative_root}'")
                self.ingestor.ensure_node_batch(
                    "Folder", {"path": str(relative_root), "name": root.name}
                )
                parent_label, parent_key, parent_val = (
                    ("Project", "name", self.project_name)
                    if parent_rel_path == Path(".")
                    else (
                        ("Package", "qualified_name", parent_container_qn)
                        if parent_container_qn
                        else ("Folder", "path", str(parent_rel_path))
                    )
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_label, parent_key, parent_val),
                    "CONTAINS_FOLDER",
                    ("Folder", "path", str(relative_root)),
                )

    def _process_files(self) -> None:
        """Second pass: Walks the directory again to process all files."""
        for root_str, dirs, files in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)
            parent_container_qn = self.structural_elements.get(relative_root)

            parent_label, parent_key, parent_val = (
                ("Package", "qualified_name", parent_container_qn)
                if parent_container_qn
                else (
                    ("Folder", "path", str(relative_root))
                    if relative_root != Path(".")
                    else ("Project", "name", self.project_name)
                )
            )

            for file_name in files:
                filepath = root / file_name
                relative_filepath = str(filepath.relative_to(self.repo_path))

                # Create generic File node for all files
                self.ingestor.ensure_node_batch(
                    "File",
                    {
                        "path": relative_filepath,
                        "name": file_name,
                        "extension": filepath.suffix,
                    },
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_label, parent_key, parent_val),
                    "CONTAINS_FILE",
                    ("File", "path", relative_filepath),
                )

                if file_name.endswith(".py"):
                    self.parse_and_ingest_file(filepath)
                elif file_name == "pyproject.toml":
                    self._parse_dependencies(filepath)

    def _get_docstring(self, node: Node) -> Optional[str]:
        """Extracts the docstring from a function or class node's body."""
        body_node = node.child_by_field_name("body")
        if not body_node or not body_node.children:
            return None
        first_statement = body_node.children[0]
        if (
            first_statement.type == "expression_statement"
            and first_statement.children[0].type == "string"
        ):
            return first_statement.children[0].text.decode("utf-8").strip("'\" \n")
        return None

    def parse_and_ingest_file(self, file_path: Path):
        relative_path = file_path.relative_to(self.repo_path)
        relative_path_str = str(relative_path)
        logger.info(f"Parsing: {relative_path_str}")

        try:
            source_bytes = file_path.read_bytes()
            tree = self.parser.parse(source_bytes)
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
            parent_container_qn = self.structural_elements.get(parent_rel_path)
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

            self._ingest_top_level_functions(root_node, module_qn)
            self._ingest_classes_and_methods(root_node, module_qn)

        except Exception as e:
            logger.error(f"Failed to parse or ingest {file_path}: {e}", exc_info=True)

    def _ingest_top_level_functions(self, root_node, parent_qn: str):
        captures = self.queries["functions"].captures(root_node)
        if "function" in captures:
            for func_node in captures["function"]:
                # Get the function name
                name_node = func_node.child_by_field_name("name")
                if not name_node:
                    continue
                func_name = name_node.text.decode("utf8")

                # Build qualified name based on nesting context
                func_qn = self._build_nested_qualified_name(
                    func_node, parent_qn, func_name
                )

                # Skip if this is a method (will be handled by class processing) or if qn is None
                if func_qn is None or self._is_method(func_node):
                    continue

                props = {
                    "qualified_name": func_qn,
                    "name": func_name,
                    "decorators": [],
                    "start_line": func_node.start_point[0] + 1,
                    "end_line": func_node.end_point[0] + 1,
                    "docstring": self._get_docstring(func_node),
                }
                logger.info(f"  Found Function: {func_name} (qn: {func_qn})")
                self.ingestor.ensure_node_batch("Function", props)

                # Link to the appropriate parent (Module for top-level, Function for nested)
                parent_type, parent_key = self._determine_function_parent(
                    func_node, parent_qn
                )
                self.ingestor.ensure_relationship_batch(
                    (parent_type, "qualified_name", parent_key),
                    "DEFINES",
                    ("Function", "qualified_name", func_qn),
                )

    def _build_nested_qualified_name(
        self, func_node, module_qn: str, func_name: str
    ) -> str:
        """Build qualified name for nested functions by traversing parent hierarchy."""
        path_parts = []
        current = func_node.parent

        # Traverse up the AST to build the nesting hierarchy
        while current and current.type != "module":
            if current.type in self.lang_config.function_node_types:
                # Parent is a function - get its name
                name_node = current.child_by_field_name("name")
                if name_node:
                    path_parts.append(name_node.text.decode("utf8"))
            elif current.type in self.lang_config.class_node_types:
                # Parent is a class - this is a method, not a nested function
                return None  # Will be handled as method
            current = current.parent

        # Reverse to get correct order (innermost to outermost)
        path_parts.reverse()

        # Build the qualified name: module.parent_func.nested_func
        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{func_name}"
        else:
            return f"{module_qn}.{func_name}"

    def _is_method(self, func_node) -> bool:
        """Check if a function is actually a method (inside a class)."""
        current = func_node.parent
        while current and current.type != "module":
            if current.type in self.lang_config.class_node_types:
                return True
            current = current.parent
        return False

    def _determine_function_parent(self, func_node, module_qn: str) -> tuple[str, str]:
        """Determine the parent entity for linking relationships."""
        current = func_node.parent

        # Look for immediate parent function
        while current and current.type != "module":
            if current.type in self.lang_config.function_node_types:
                name_node = current.child_by_field_name("name")
                if name_node:
                    parent_func_name = name_node.text.decode("utf8")
                    # Build parent function's qualified name
                    parent_qn = self._build_nested_qualified_name(
                        current, module_qn, parent_func_name
                    )
                    if parent_qn:
                        return "Function", parent_qn
                break
            current = current.parent

        # Default to module parent
        return "Module", module_qn

    def _ingest_classes_and_methods(self, root_node, parent_qn: str):
        class_captures = self.queries["classes"].captures(root_node)
        if "class" in class_captures:
            for class_node in class_captures["class"]:
                name_node = class_node.child_by_field_name("name")
                if not name_node:
                    continue
                class_name = name_node.text.decode("utf8")
                class_qn = f"{parent_qn}.{class_name}"
                class_props = {
                    "qualified_name": class_qn,
                    "name": class_name,
                    "decorators": [],
                    "start_line": class_node.start_point[0] + 1,
                    "end_line": class_node.end_point[0] + 1,
                    "docstring": self._get_docstring(class_node),
                }
                logger.info(f"  Found Class: {class_name} (qn: {class_qn})")
                self.ingestor.ensure_node_batch("Class", class_props)
                self.ingestor.ensure_relationship_batch(
                    ("Module", "qualified_name", parent_qn),
                    "DEFINES",
                    ("Class", "qualified_name", class_qn),
                )
                body_node = class_node.child_by_field_name("body")
                if not body_node:
                    continue
                method_captures = self.queries["functions"].captures(body_node)
                if "function" in method_captures:
                    for method_node in method_captures["function"]:
                        method_name_node = method_node.child_by_field_name("name")
                        if not method_name_node:
                            continue
                        method_name = method_name_node.text.decode("utf8")
                        method_qn = f"{class_qn}.{method_name}"
                        method_props = {
                            "qualified_name": method_qn,
                            "name": method_name,
                            "decorators": [],
                            "start_line": method_node.start_point[0] + 1,
                            "end_line": method_node.end_point[0] + 1,
                            "docstring": self._get_docstring(method_node),
                        }
                        logger.info(
                            f"    Found Method: {method_name} (qn: {method_qn})"
                        )
                        self.ingestor.ensure_node_batch("Method", method_props)
                        self.ingestor.ensure_relationship_batch(
                            ("Class", "qualified_name", class_qn),
                            "DEFINES_METHOD",
                            ("Method", "qualified_name", method_qn),
                        )

    def _parse_dependencies(self, filepath: Path) -> None:
        """Parses a pyproject.toml file for dependencies."""
        logger.info(f"  Parsing pyproject.toml: {filepath}")
        try:
            data = toml.load(filepath)
            # Support both Poetry and standard PEP 621 dependencies
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
