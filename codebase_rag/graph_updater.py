import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import toml
from loguru import logger
from tree_sitter import Node, Parser, QueryCursor

from codebase_rag.services.graph_service import MemgraphIngestor

from .config import IGNORE_PATTERNS
from .language_config import LanguageConfig, get_language_config


class GraphUpdater:
    """Parses code using Tree-sitter and updates the graph."""

    def __init__(
        self,
        ingestor: MemgraphIngestor,
        repo_path: Path,
        parsers: dict[str, Parser],
        queries: dict[str, Any],
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_name = repo_path.name
        self.structural_elements: dict[Path, str | None] = {}
        self.function_registry: dict[str, str] = {}  # {qualified_name: type}
        self.simple_name_lookup: dict[str, set[str]] = defaultdict(set)
        self.ast_cache: dict[Path, tuple[Node, str]] = {}
        self.import_mapping: dict[
            str, dict[str, str]
        ] = {}  # {module_qn: {local_name: fully_qualified_name}}
        # Using centralized ignore patterns from config
        self.ignore_dirs = IGNORE_PATTERNS

    def run(self) -> None:
        """Orchestrates the parsing and ingestion process."""
        self.ingestor.ensure_node_batch("Project", {"name": self.project_name})
        logger.info(f"Ensuring Project: {self.project_name}")

        logger.info("--- Pass 1: Identifying Packages and Folders ---")
        self._identify_structure()

        logger.info(
            "\n--- Pass 2: Processing Files, Caching ASTs, and Collecting Definitions ---"
        )
        self._process_files()

        logger.info(
            f"\n--- Found {len(self.function_registry)} functions/methods in codebase ---"
        )
        logger.info("--- Pass 3: Processing Function Calls from AST Cache ---")
        self._process_function_calls()

        logger.info("\n--- Analysis complete. Flushing all data to database... ---")
        self.ingestor.flush_all()

    def remove_file_from_state(self, file_path: Path) -> None:
        """Removes all state associated with a file from the updater's memory."""
        logger.debug(f"Removing in-memory state for: {file_path}")

        # Clear AST cache
        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug("  - Removed from ast_cache")

        # Determine the module qualified name prefix for the file
        relative_path = file_path.relative_to(self.repo_path)
        if file_path.name == "__init__.py":
            module_qn_prefix = ".".join(
                [self.project_name] + list(relative_path.parent.parts)
            )
        else:
            module_qn_prefix = ".".join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )

        # We need to find all qualified names that belong to this file/module
        qns_to_remove = set()

        # Clean function_registry and collect qualified names to remove
        for qn in list(self.function_registry.keys()):
            if qn.startswith(module_qn_prefix + ".") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(
                f"  - Removing {len(qns_to_remove)} QNs from function_registry"
            )

        # Clean simple_name_lookup
        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(f"  - Cleaned simple_name '{simple_name}'")

    def _identify_structure(self) -> None:
        """First pass: Walks the directory to find all packages and folders."""
        for root_str, dirs, _ in os.walk(self.repo_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]
            root = Path(root_str)
            relative_root = root.relative_to(self.repo_path)

            parent_rel_path = relative_root.parent
            parent_container_qn = self.structural_elements.get(parent_rel_path)

            # Check if this directory is a package for any supported language
            is_package = False
            package_indicators = set()

            # Collect package indicators from all language configs
            for lang_name, lang_queries in self.queries.items():
                lang_config = lang_queries["config"]
                package_indicators.update(lang_config.package_indicators)

            # Check if any package indicator exists
            for indicator in package_indicators:
                if (root / indicator).exists():
                    is_package = True
                    break

            if is_package:
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
        """Second pass: Walks the directory, parses files, and caches their ASTs."""
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

                # Check if this file type is supported for parsing
                lang_config = get_language_config(filepath.suffix)
                if lang_config and lang_config.name in self.parsers:
                    self.parse_and_ingest_file(filepath, lang_config.name)
                elif file_name == "pyproject.toml":
                    self._parse_dependencies(filepath)

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
                return text.decode("utf-8").strip("'\" \n")  # type: ignore[no-any-return]
        return None

    def parse_and_ingest_file(self, file_path: Path, language: str) -> None:
        """
        Parses a file, ingests its structure and definitions,
        and caches the AST for the next pass.
        """
        if isinstance(file_path, str):
            file_path = Path(file_path)
        relative_path = file_path.relative_to(self.repo_path)
        relative_path_str = str(relative_path)
        logger.info(f"Parsing and Caching AST for {language}: {relative_path_str}")

        try:
            # Check if language is supported
            if language not in self.parsers or language not in self.queries:
                logger.warning(f"Unsupported language '{language}' for {file_path}")
                return

            source_bytes = file_path.read_bytes()
            parser = self.parsers[language]
            tree = parser.parse(source_bytes)
            root_node = tree.root_node

            # Cache the parsed AST for the function call pass
            self.ast_cache[file_path] = (root_node, language)

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

            self._parse_imports(root_node, module_qn, language)
            self._ingest_top_level_functions(root_node, module_qn, language)
            self._ingest_classes_and_methods(root_node, module_qn, language)

        except Exception as e:
            logger.error(f"Failed to parse or ingest {file_path}: {e}")

    def _parse_imports(self, root_node: Node, module_qn: str, language: str) -> None:
        """Parse import statements and build import mapping for the module."""
        if language not in self.queries or not self.queries[language].get("imports"):
            return

        lang_config = self.queries[language]["config"]
        imports_query = self.queries[language]["imports"]

        self.import_mapping[module_qn] = {}

        try:
            from tree_sitter import QueryCursor

            cursor = QueryCursor(imports_query)
            captures = cursor.captures(root_node)

            # Handle different language import patterns
            if language == "python":
                self._parse_python_imports(captures, module_qn)
            elif language in ["javascript", "typescript"]:
                self._parse_js_ts_imports(captures, module_qn)
            elif language == "java":
                self._parse_java_imports(captures, module_qn)
            elif language == "rust":
                self._parse_rust_imports(captures, module_qn)
            elif language == "go":
                self._parse_go_imports(captures, module_qn)
            else:
                # Generic fallback for other languages
                self._parse_generic_imports(captures, module_qn, lang_config)

            logger.debug(
                f"Parsed {len(self.import_mapping[module_qn])} imports in {module_qn}"
            )

        except Exception as e:
            logger.warning(f"Failed to parse imports in {module_qn}: {e}")

    def _parse_python_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Python import statements with full support for all import types."""
        for import_node in captures.get("import", []) + captures.get("import_from", []):
            if import_node.type == "import_statement":
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == "import_from_statement":
                self._handle_python_import_from_statement(import_node, module_qn)

    def _handle_python_import_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        """Handle 'import module' statements."""
        for child in import_node.children:
            if child.type == "dotted_name":
                module_name = child.text.decode("utf-8")
                parts = module_name.split(".")
                local_name = parts[-1]  # Last part becomes the local name
                full_name = f"{self.project_name}.{module_name}"
                self.import_mapping[module_qn][local_name] = full_name
                logger.debug(f"  Import: {local_name} -> {full_name}")
            elif child.type == "aliased_import":
                # Handle 'import module as alias'
                module_name = None
                alias = None
                for grandchild in child.children:
                    if grandchild.type == "dotted_name":
                        module_name = grandchild.text.decode("utf-8")
                    elif grandchild.type == "identifier":
                        alias = grandchild.text.decode("utf-8")

                if module_name and alias:
                    full_name = f"{self.project_name}.{module_name}"
                    self.import_mapping[module_qn][alias] = full_name
                    logger.debug(f"  Aliased import: {alias} -> {full_name}")

    def _handle_python_import_from_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        """Handle 'from module import name' statements."""
        module_name = None
        imported_names = []

        for child in import_node.children:
            if child.type == "dotted_name" and module_name is None:
                # First dotted_name is the module
                module_name = child.text.decode("utf-8")
            elif child.type == "dotted_name" and module_name is not None:
                # Subsequent dotted_names are imported items
                imported_names.append(child.text.decode("utf-8"))
            elif child.type == "relative_import":
                # Handle relative imports like 'from .module import name'
                module_name = self._resolve_relative_import(child, module_qn)

        if module_name:
            base_module = (
                f"{self.project_name}.{module_name}"
                if not module_name.startswith(self.project_name)
                else module_name
            )
            for imported_name in imported_names:
                full_name = f"{base_module}.{imported_name}"
                self.import_mapping[module_qn][imported_name] = full_name
                logger.debug(f"  From import: {imported_name} -> {full_name}")

    def _resolve_relative_import(self, relative_node: Node, module_qn: str) -> str:
        """Resolve relative imports like '.module' or '..parent.module'."""
        module_parts = module_qn.split(".")[1:]  # Remove project name

        # Count the dots to determine how many levels to go up
        dots = 0
        module_name = ""

        for child in relative_node.children:
            if child.type == "import_prefix":
                dots = len(child.text.decode("utf-8"))
            elif child.type == "dotted_name":
                module_name = child.text.decode("utf-8")

        # Calculate the target module
        if dots == 1:  # from .module
            target_parts = module_parts[:-1]  # Current package
        else:  # from ..module (go up dots-1 levels)
            target_parts = module_parts[: -(dots - 1)] if dots > 1 else module_parts

        if module_name:
            target_parts.extend(module_name.split("."))

        return ".".join(target_parts)

    def _parse_js_ts_imports(self, captures: dict, module_qn: str) -> None:
        """Parse JavaScript/TypeScript import statements."""
        if module_qn not in self.import_mapping:
            self.import_mapping[module_qn] = {}

        for import_node in captures.get("import", []):
            if import_node.type == "import_statement":
                # Find the source module
                source_module = None
                for child in import_node.children:
                    if child.type == "string":
                        # Extract module path from string (remove quotes)
                        source_text = child.text.decode("utf-8").strip("'\"")
                        source_module = self._resolve_js_module_path(
                            source_text, module_qn
                        )
                        break

                if not source_module:
                    continue

                # Parse import clause to extract imported names
                for child in import_node.children:
                    if child.type == "import_clause":
                        self._parse_js_import_clause(child, source_module, module_qn)

            elif import_node.type == "lexical_declaration":
                # Handle CommonJS require() statements
                self._parse_js_require(import_node, module_qn)

    def _resolve_js_module_path(self, import_path: str, current_module: str) -> str:
        """Resolve JavaScript module path to qualified name."""
        if import_path.startswith("./") or import_path.startswith("../"):
            # Relative import - resolve relative to current module
            current_parts = current_module.split(".")
            if import_path.startswith("./"):
                # Same directory
                base_parts = current_parts[:-1]  # Remove file name
                rel_path = import_path[2:]  # Remove './'
            else:
                # Parent directory(s)
                dots = 0
                for char in import_path:
                    if char == ".":
                        dots += 1
                    elif char == "/":
                        break
                levels_up = (dots - 1) // 2  # Each '../' is 2 dots and a slash
                base_parts = current_parts[: -(levels_up + 1)]
                rel_path = import_path[levels_up * 3 + 1 :]  # Remove '../' parts

            if rel_path:
                base_parts.extend(rel_path.replace("/", ".").split("."))
            return ".".join(base_parts)
        else:
            # Absolute import (package)
            return import_path.replace("/", ".")

    def _parse_js_import_clause(
        self, clause_node: Node, source_module: str, current_module: str
    ) -> None:
        """Parse JavaScript import clause (named, default, namespace imports)."""
        for child in clause_node.children:
            if child.type == "identifier":
                # Default import: import React from 'react'
                imported_name = child.text.decode("utf-8")
                self.import_mapping[current_module][imported_name] = (
                    f"{source_module}.default"
                )
                logger.debug(
                    f"JS default import: {imported_name} -> {source_module}.default"
                )

            elif child.type == "named_imports":
                # Named imports: import { func1, func2 } from './module'
                for grandchild in child.children:
                    if grandchild.type == "import_specifier":
                        # Get the imported name
                        for spec_child in grandchild.children:
                            if spec_child.type == "identifier":
                                imported_name = spec_child.text.decode("utf-8")
                                self.import_mapping[current_module][imported_name] = (
                                    f"{source_module}.{imported_name}"
                                )
                                logger.debug(
                                    f"JS named import: {imported_name} -> {source_module}.{imported_name}"
                                )
                                break

            elif child.type == "namespace_import":
                # Namespace import: import * as utils from './utils'
                for grandchild in child.children:
                    if grandchild.type == "identifier":
                        namespace_name = grandchild.text.decode("utf-8")
                        self.import_mapping[current_module][namespace_name] = (
                            source_module
                        )
                        logger.debug(
                            f"JS namespace import: {namespace_name} -> {source_module}"
                        )
                        break

    def _parse_js_require(self, decl_node: Node, current_module: str) -> None:
        """Parse CommonJS require() statements."""
        # Look for: const name = require('module')
        var_name = None
        required_module = None

        for child in decl_node.children:
            if child.type == "variable_declarator":
                for grandchild in child.children:
                    if grandchild.type == "identifier":
                        var_name = grandchild.text.decode("utf-8")
                    elif grandchild.type == "call_expression":
                        # Check if it's require()
                        for call_child in grandchild.children:
                            if (
                                call_child.type == "identifier"
                                and call_child.text.decode("utf-8") == "require"
                            ):
                                # Find the argument
                                for arg_child in grandchild.children:
                                    if arg_child.type == "arguments":
                                        for arg in arg_child.children:
                                            if arg.type == "string":
                                                required_module = arg.text.decode(
                                                    "utf-8"
                                                ).strip("'\"")
                                                break

        if var_name and required_module:
            resolved_module = self._resolve_js_module_path(
                required_module, current_module
            )
            self.import_mapping[current_module][var_name] = resolved_module
            logger.debug(f"JS require: {var_name} -> {resolved_module}")

    def _parse_java_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Java import statements."""
        if module_qn not in self.import_mapping:
            self.import_mapping[module_qn] = {}

        for import_node in captures.get("import", []):
            if import_node.type == "import_declaration":
                is_static = False
                imported_path = None
                is_wildcard = False

                # Parse import declaration
                for child in import_node.children:
                    if child.type == "static":
                        is_static = True
                    elif child.type == "scoped_identifier":
                        imported_path = child.text.decode("utf-8")
                    elif child.type == "asterisk":
                        is_wildcard = True

                if not imported_path:
                    continue

                if is_wildcard:
                    # import java.util.*; - wildcard import
                    # For wildcard imports, we can't pre-map specific names
                    # but we can store the package for later resolution
                    logger.debug(f"Java wildcard import: {imported_path}.*")
                    # Store wildcard import for potential future use
                    self.import_mapping[module_qn][f"*{imported_path}"] = imported_path
                else:
                    # import java.util.List; or import static java.lang.Math.PI;
                    parts = imported_path.split(".")
                    if parts:
                        imported_name = parts[-1]  # Last part is the class/method name
                        if is_static:
                            # Static import - method/field can be used directly
                            self.import_mapping[module_qn][imported_name] = (
                                imported_path
                            )
                            logger.debug(
                                f"Java static import: {imported_name} -> {imported_path}"
                            )
                        else:
                            # Regular class import
                            self.import_mapping[module_qn][imported_name] = (
                                imported_path
                            )
                            logger.debug(
                                f"Java import: {imported_name} -> {imported_path}"
                            )

    def _parse_rust_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Rust use declarations."""
        if module_qn not in self.import_mapping:
            self.import_mapping[module_qn] = {}

        for import_node in captures.get("import", []):
            if import_node.type == "use_declaration":
                self._parse_rust_use_declaration(import_node, module_qn)

    def _parse_rust_use_declaration(self, use_node: Node, module_qn: str) -> None:
        """Parse a single Rust use declaration."""
        for child in use_node.children:
            if child.type == "scoped_identifier":
                # Simple use: use std::collections::HashMap;
                full_path = child.text.decode("utf-8")
                parts = full_path.split("::")
                if parts:
                    imported_name = parts[-1]
                    self.import_mapping[module_qn][imported_name] = full_path
                    logger.debug(f"Rust use: {imported_name} -> {full_path}")

            elif child.type == "use_as_clause":
                # Aliased use: use std::collections::HashMap as Map;
                original_path = None
                alias_name = None
                for grandchild in child.children:
                    if grandchild.type == "scoped_identifier":
                        original_path = grandchild.text.decode("utf-8")
                    elif grandchild.type == "identifier":
                        alias_name = grandchild.text.decode("utf-8")

                if original_path and alias_name:
                    self.import_mapping[module_qn][alias_name] = original_path
                    logger.debug(f"Rust use as: {alias_name} -> {original_path}")

            elif child.type == "scoped_use_list":
                # Multiple use: use std::{fs, io};
                base_path = None
                imported_names = []

                for grandchild in child.children:
                    if grandchild.type == "identifier":
                        base_path = grandchild.text.decode("utf-8")
                    elif grandchild.type == "use_list":
                        # Extract names from the list
                        for list_child in grandchild.children:
                            if list_child.type == "identifier":
                                imported_names.append(list_child.text.decode("utf-8"))

                if base_path:
                    for name in imported_names:
                        full_path = f"{base_path}::{name}"
                        self.import_mapping[module_qn][name] = full_path
                        logger.debug(f"Rust use list: {name} -> {full_path}")

            elif child.type == "use_wildcard":
                # Glob use: use crate::utils::*;
                for grandchild in child.children:
                    if (
                        grandchild.type == "scoped_identifier"
                        or grandchild.type == "crate"
                    ):
                        base_path = grandchild.text.decode("utf-8")
                        # Store wildcard import for potential future use
                        self.import_mapping[module_qn][f"*{base_path}"] = base_path
                        logger.debug(f"Rust glob use: {base_path}::*")

    def _parse_go_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Go import declarations."""
        if module_qn not in self.import_mapping:
            self.import_mapping[module_qn] = {}

        for import_node in captures.get("import", []):
            if import_node.type == "import_declaration":
                # Handle both single and multiple imports
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        """Parse a Go import declaration."""
        for child in import_node.children:
            if child.type == "import_spec":
                # Single import or import in a list
                self._parse_go_import_spec(child, module_qn)
            elif child.type == "import_spec_list":
                # Multiple imports in parentheses
                for grandchild in child.children:
                    if grandchild.type == "import_spec":
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        """Parse a single Go import spec."""
        alias_name = None
        import_path = None

        for child in spec_node.children:
            if child.type == "package_identifier":
                # Aliased import: import f "fmt"
                alias_name = child.text.decode("utf-8")
            elif child.type == "interpreted_string_literal":
                # Extract import path from string literal
                import_path = child.text.decode("utf-8").strip('"')

        if import_path:
            # Determine the package name
            if alias_name:
                # Explicit alias
                package_name = alias_name
            else:
                # Use last part of path as package name
                parts = import_path.split("/")
                package_name = parts[-1] if parts else import_path

            # Map package name to full import path
            self.import_mapping[module_qn][package_name] = import_path
            logger.debug(f"Go import: {package_name} -> {import_path}")

    def _parse_generic_imports(
        self, captures: dict, module_qn: str, lang_config: LanguageConfig
    ) -> None:
        """Generic fallback import parsing for other languages."""
        if module_qn not in self.import_mapping:
            self.import_mapping[module_qn] = {}

        for import_node in captures.get("import", []):
            logger.debug(
                f"Generic import parsing for {lang_config.name}: {import_node.type}"
            )

    def _ingest_top_level_functions(
        self, root_node: Node, module_qn: str, language: str
    ) -> None:
        lang_queries = self.queries[language]
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
            func_qn = f"{module_qn}.{func_name}"

            # Extract function properties
            func_props: dict[str, Any] = {
                "qualified_name": func_qn,
                "name": func_name,
                "decorators": [],
                "start_line": func_node.start_point[0] + 1,
                "end_line": func_node.end_point[0] + 1,
                "docstring": self._get_docstring(func_node),
            }
            logger.info(f"  Found Function: {func_name} (qn: {func_qn})")
            self.ingestor.ensure_node_batch("Function", func_props)

            self.function_registry[func_qn] = "Function"
            self.simple_name_lookup[func_name].add(func_qn)

            # Link Function to Module
            self.ingestor.ensure_relationship_batch(
                ("Module", "qualified_name", module_qn),
                "DEFINES",
                ("Function", "qualified_name", func_qn),
            )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageConfig,
    ) -> str | None:
        path_parts = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                f"Unexpected parent type for node {func_node}: {type(current)}. Skipping."
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
        self, root_node: Node, module_qn: str, language: str
    ) -> None:
        lang_queries = self.queries[language]

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
            class_props: dict[str, Any] = {
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
                ("Module", "qualified_name", module_qn),
                "DEFINES",
                ("Class", "qualified_name", class_qn),
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
                method_props: dict[str, Any] = {
                    "qualified_name": method_qn,
                    "name": method_name,
                    "decorators": [],
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

    def _parse_dependencies(self, filepath: Path) -> None:
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

    def _process_function_calls(self) -> None:
        """Third pass: Process function calls using the cached ASTs."""
        for file_path, (root_node, language) in self.ast_cache.items():
            self._process_calls_in_file(file_path, root_node, language)

    def _process_calls_in_file(
        self, file_path: Path, root_node: Node, language: str
    ) -> None:
        """Process function calls in a specific file using its cached AST."""
        relative_path = file_path.relative_to(self.repo_path)
        logger.debug(f"Processing calls in cached AST for: {relative_path}")

        try:
            module_qn = ".".join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name == "__init__.py":
                module_qn = ".".join(
                    [self.project_name] + list(relative_path.parent.parts)
                )

            self._process_calls_in_functions(root_node, module_qn, language)
            self._process_calls_in_classes(root_node, module_qn, language)

        except Exception as e:
            logger.error(f"Failed to process calls in {file_path}: {e}")

    def _process_calls_in_functions(
        self, root_node: Node, module_qn: str, language: str
    ) -> None:
        lang_queries = self.queries[language]
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
                    func_node, func_qn, "Function", module_qn, language
                )

    def _process_calls_in_classes(
        self, root_node: Node, module_qn: str, language: str
    ) -> None:
        lang_queries = self.queries[language]

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
                    method_node, method_qn, "Method", module_qn, language
                )

    def _get_call_target_name(self, call_node: Node) -> str | None:
        """Extracts the name of the function or method being called."""
        # For 'call' in Python and 'call_expression' in JS/TS
        if func_child := call_node.child_by_field_name("function"):
            if func_child.type == "identifier":
                text = func_child.text
                if text is not None:
                    return text.decode("utf8")  # type: ignore[no-any-return]
            # Python: obj.method() -> attribute
            elif func_child.type == "attribute":
                if attr_child := func_child.child_by_field_name("attribute"):
                    text = attr_child.text
                    if text is not None:
                        return text.decode("utf8")  # type: ignore[no-any-return]
            # JS/TS: obj.method() -> member_expression
            elif func_child.type == "member_expression":
                if prop_child := func_child.child_by_field_name("property"):
                    text = prop_child.text
                    if text is not None:
                        return text.decode("utf8")  # type: ignore[no-any-return]

        # For 'method_invocation' in Java
        if name_node := call_node.child_by_field_name("name"):
            text = name_node.text
            if text is not None:
                return text.decode("utf8")  # type: ignore[no-any-return]

        return None

    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: str,
    ) -> None:
        calls_query = self.queries[language].get("calls")
        if not calls_query:
            return

        cursor = QueryCursor(calls_query)
        captures = cursor.captures(caller_node)
        call_nodes = captures.get("call", [])

        for call_node in call_nodes:
            if not isinstance(call_node, Node):
                continue
            call_name = self._get_call_target_name(call_node)
            if not call_name:
                continue

            callee_info = self._resolve_function_call(call_name, module_qn)
            if not callee_info:
                continue

            callee_type, callee_qn = callee_info
            logger.debug(
                f"      Found call from {caller_qn} to {call_name} (resolved as {callee_type}:{callee_qn})"
            )

            self.ingestor.ensure_relationship_batch(
                (caller_type, "qualified_name", caller_qn),
                "CALLS",
                (callee_type, "qualified_name", callee_qn),
            )

    def _resolve_function_call(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # Phase 1: Check import mapping for 100% accurate resolution
        if module_qn in self.import_mapping:
            import_map = self.import_mapping[module_qn]
            if call_name in import_map:
                imported_qn = import_map[call_name]
                if imported_qn in self.function_registry:
                    logger.debug(f"Import-resolved call: {call_name} -> {imported_qn}")
                    return self.function_registry[imported_qn], imported_qn
                # Check if it's a method call on imported class
                for registered_qn in self.function_registry:
                    if registered_qn.startswith(f"{imported_qn}."):
                        # This might be a method call like User.get_name where User was imported
                        method_name = registered_qn[len(imported_qn) + 1 :]
                        if method_name == call_name:
                            logger.debug(
                                f"Import-resolved method call: {call_name} -> {registered_qn}"
                            )
                            return self.function_registry[registered_qn], registered_qn

        # Phase 2: Try to resolve with fully qualified names in order of likelihood
        module_parts = module_qn.split(".")
        possible_qns = []

        # 1. Same module (most common for local calls)
        possible_qns.append(f"{module_qn}.{call_name}")

        # 2. Parent modules (for sibling imports)
        for i in range(len(module_parts) - 1, 0, -1):
            parent_module = ".".join(module_parts[:i])
            possible_qns.append(f"{parent_module}.{call_name}")

        # 3. All submodules of parent modules (for cross-package imports)
        for i in range(len(module_parts) - 1, 0, -1):
            parent_module = ".".join(module_parts[:i])
            # Check all registered functions that start with this parent
            for registered_qn in self.function_registry:
                if registered_qn.startswith(
                    f"{parent_module}."
                ) and registered_qn.endswith(f".{call_name}"):
                    possible_qns.append(registered_qn)

        # Remove duplicates while preserving order
        seen = set()
        unique_qns = []
        for qn in possible_qns:
            if qn not in seen:
                seen.add(qn)
                unique_qns.append(qn)

        # Try each possible qualified name
        for qn in unique_qns:
            if qn in self.function_registry:
                return self.function_registry[qn], qn

        # Phase 3: If not found with FQN, use simple name lookup with improved matching
        if call_name in self.simple_name_lookup:
            candidates = list(self.simple_name_lookup[call_name])

            # Sort candidates by likelihood (prioritize closer modules)
            candidates.sort(
                key=lambda qn: self._calculate_import_distance(qn, module_qn)
            )

            # Return the most likely candidate
            if candidates:
                best_candidate = candidates[0]
                return self.function_registry[best_candidate], best_candidate

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

        # Bonus for same package (sibling modules)
        if len(caller_parts) > 1 and len(candidate_parts) > 1:
            if (
                caller_parts[:-1] == candidate_parts[:-2]
            ):  # Same package, different module
                base_distance -= 2

        # Bonus for parent-child relationship
        if candidate_qn.startswith(".".join(caller_parts[:-1]) + "."):
            base_distance -= 1

        return base_distance
