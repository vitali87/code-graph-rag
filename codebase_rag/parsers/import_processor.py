"""Import processor for parsing and resolving import statements."""

from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..language_config import LanguageConfig

# Common language constants for performance optimization
_JS_TYPESCRIPT_LANGUAGES = {"javascript", "typescript"}


class ImportProcessor:
    """Handles parsing and processing of import statements."""

    def __init__(
        self,
        repo_path_getter: Any,
        project_name_getter: Any,
        ingestor: Any | None = None,
    ) -> None:
        self._repo_path_getter = repo_path_getter
        self._project_name_getter = project_name_getter
        self.ingestor = ingestor
        self.import_mapping: dict[str, dict[str, str]] = {}

    @property
    def repo_path(self) -> Path:
        """Get the current repo path dynamically."""
        if callable(self._repo_path_getter):
            result = self._repo_path_getter()
            return result if isinstance(result, Path) else Path(result)
        return (
            Path(self._repo_path_getter)
            if isinstance(self._repo_path_getter, str)
            else self._repo_path_getter
        )

    @property
    def project_name(self) -> str:
        """Get the current project name dynamically."""
        if callable(self._project_name_getter):
            result = self._project_name_getter()
            return str(result)
        return str(self._project_name_getter)

    def parse_imports(
        self, root_node: Node, module_qn: str, language: str, queries: dict[str, Any]
    ) -> None:
        """Parse import statements and build import mapping for the module."""
        if language not in queries or not queries[language].get("imports"):
            return

        lang_config = queries[language]["config"]
        imports_query = queries[language]["imports"]

        self.import_mapping[module_qn] = {}

        try:
            cursor = QueryCursor(imports_query)
            captures = cursor.captures(root_node)

            # Handle different language import patterns
            if language == "python":
                self._parse_python_imports(captures, module_qn)
            elif language in _JS_TYPESCRIPT_LANGUAGES:
                self._parse_js_ts_imports(captures, module_qn)
            elif language == "java":
                self._parse_java_imports(captures, module_qn)
            elif language == "rust":
                self._parse_rust_imports(captures, module_qn)
            elif language == "go":
                self._parse_go_imports(captures, module_qn)
            elif language == "cpp":
                self._parse_cpp_imports(captures, module_qn)
            else:
                # Generic fallback for other languages
                self._parse_generic_imports(captures, module_qn, lang_config)

            logger.debug(
                f"Parsed {len(self.import_mapping[module_qn])} imports in {module_qn}"
            )

            # Create IMPORTS relationships for each parsed import
            if self.ingestor and module_qn in self.import_mapping:
                for local_name, full_name in self.import_mapping[module_qn].items():
                    self.ingestor.ensure_relationship_batch(
                        ("Module", "qualified_name", module_qn),
                        "IMPORTS",
                        ("Module", "qualified_name", full_name),
                    )
                    logger.debug(
                        f"  Created IMPORTS relationship: {module_qn} -> {full_name}"
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
        for child in import_node.named_children:
            if child.type == "dotted_name":
                module_name = child.text.decode("utf-8") if child.text else ""
                # For 'import a.b.c', the local name available in the scope is 'a'
                local_name = module_name.split(".")[0]

                # Check if it's a local module to prefix with project name
                if (self.repo_path / local_name).is_dir() or (
                    self.repo_path / f"{local_name}.py"
                ).is_file():
                    full_name = f"{self.project_name}.{module_name}"
                else:
                    full_name = module_name  # For stdlib or third-party

                self.import_mapping[module_qn][local_name] = full_name
                logger.debug(f"  Import: {local_name} -> {full_name}")
            elif child.type == "aliased_import":
                # Handle 'import module as alias'
                module_name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")

                if (
                    module_name_node
                    and alias_node
                    and module_name_node.text
                    and alias_node.text
                ):
                    module_name = module_name_node.text.decode("utf-8")
                    alias = alias_node.text.decode("utf-8")

                    # Determine the fully qualified name of the imported module
                    top_level_module = module_name.split(".")[0]
                    if (self.repo_path / top_level_module).is_dir() or (
                        self.repo_path / f"{top_level_module}.py"
                    ).is_file():
                        full_name = f"{self.project_name}.{module_name}"
                    else:
                        full_name = module_name

                    self.import_mapping[module_qn][alias] = full_name
                    logger.debug(f"  Aliased import: {alias} -> {full_name}")

    def _handle_python_import_from_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        """Handle 'from module import name' statements."""
        # Use field-based parsing for robustness
        module_name_node = import_node.child_by_field_name("module_name")
        if not module_name_node:
            return

        # Extract module name
        if module_name_node.type == "dotted_name":
            module_name = module_name_node.text.decode("utf-8")
        elif module_name_node.type == "relative_import":
            module_name = self._resolve_relative_import(module_name_node, module_qn)
        else:
            return

        # Extract imported items - check both field names and direct children
        imported_items = []
        is_wildcard = False

        # First check field-named children (regular imports)
        for name_node in import_node.children_by_field_name("name"):
            if name_node.type == "dotted_name":
                # Simple import: from module import name
                name = name_node.text.decode("utf-8")
                imported_items.append((name, name))
            elif name_node.type == "aliased_import":
                # Aliased import: from module import name as alias
                original_name_node = name_node.child_by_field_name("name")
                alias_node = name_node.child_by_field_name("alias")
                if original_name_node and alias_node:
                    original_name = original_name_node.text.decode("utf-8")
                    alias = alias_node.text.decode("utf-8")
                    imported_items.append((alias, original_name))

        # Check for wildcard imports (direct children, not in "name" field)
        for child in import_node.children:
            if child.type == "wildcard_import":
                # Wildcard import: from module import *
                is_wildcard = True
                break

        if module_name and (imported_items or is_wildcard):
            # Only prepend project name for local modules
            if module_name.startswith(self.project_name):
                base_module = module_name
            else:
                top_level_module = module_name.split(".")[0]
                if (self.repo_path / top_level_module).is_dir() or (
                    self.repo_path / f"{top_level_module}.py"
                ).is_file():
                    base_module = f"{self.project_name}.{module_name}"
                else:
                    base_module = module_name

            if is_wildcard:
                # Handle wildcard import: from module import *
                wildcard_key = f"*{base_module}"
                self.import_mapping[module_qn][wildcard_key] = base_module
                logger.debug(f"  Wildcard import: * -> {base_module}")
            else:
                # Handle regular imports
                for local_name, original_name in imported_items:
                    full_name = f"{base_module}.{original_name}"
                    self.import_mapping[module_qn][local_name] = full_name
                    logger.debug(f"  From import: {local_name} -> {full_name}")

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

        # Calculate the target module - dots corresponds to levels to go up
        target_parts = module_parts[:-dots] if dots > 0 else module_parts

        if module_name:
            target_parts.extend(module_name.split("."))

        return ".".join(target_parts)

    def _parse_js_ts_imports(self, captures: dict, module_qn: str) -> None:
        """Parse JavaScript/TypeScript import statements."""

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

            elif import_node.type == "export_statement":
                # Handle re-export statements like: export { name } from './module'
                self._parse_js_reexport(import_node, module_qn)

    def _resolve_js_module_path(self, import_path: str, current_module: str) -> str:
        """Resolve JavaScript module path to qualified name."""
        if not import_path.startswith("."):
            # Absolute import (package)
            return import_path.replace("/", ".")

        # Relative import - resolve relative to current module
        current_parts = current_module.split(".")[
            :-1
        ]  # Start from the current directory
        import_parts = import_path.split("/")

        for part in import_parts:
            if part == ".":
                continue  # Stays in the current directory
            if part == "..":
                if current_parts:
                    current_parts.pop()  # Go up one level
            elif part:
                current_parts.append(part)

        return ".".join(current_parts)

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
                        # Handle both simple imports and aliased imports
                        # Simple: import { name } from 'module'
                        # Aliased: import { name as alias } from 'module'

                        name_node = grandchild.child_by_field_name("name")
                        alias_node = grandchild.child_by_field_name("alias")
                        if name_node and name_node.text:
                            imported_name = name_node.text.decode("utf-8")
                            local_name = (
                                alias_node.text.decode("utf-8")
                                if alias_node and alias_node.text
                                else imported_name
                            )
                            self.import_mapping[current_module][local_name] = (
                                f"{source_module}.{imported_name}"
                            )
                            logger.debug(
                                f"JS named import: {local_name} -> "
                                f"{source_module}.{imported_name}"
                            )

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
        """Parse CommonJS require() statements using field-based access."""
        # Look for: const/let/var name = require('module')
        for declarator in decl_node.children:
            if declarator.type == "variable_declarator":
                # Use field-based access for robustness
                name_node = declarator.child_by_field_name("name")
                value_node = declarator.child_by_field_name("value")

                if (
                    name_node
                    and value_node
                    and name_node.type == "identifier"
                    and value_node.type == "call_expression"
                ):
                    # Check if it's require()
                    func_node = value_node.child_by_field_name("function")
                    args_node = value_node.child_by_field_name("arguments")

                    if (
                        func_node
                        and args_node
                        and func_node.type == "identifier"
                        and func_node.text.decode("utf-8") == "require"
                    ):
                        # Extract module path from first argument
                        for arg in args_node.children:
                            if arg.type == "string":
                                var_name = name_node.text.decode("utf-8")
                                required_module = arg.text.decode("utf-8").strip("'\"")

                                resolved_module = self._resolve_js_module_path(
                                    required_module, current_module
                                )
                                self.import_mapping[current_module][var_name] = (
                                    resolved_module
                                )
                                logger.debug(
                                    f"JS require: {var_name} -> {resolved_module}"
                                )
                                break

    def _parse_js_reexport(self, export_node: Node, current_module: str) -> None:
        """Parse JavaScript re-export statements like 'export { name } from './module'."""
        # Find the source module in export statement
        source_module = None
        for child in export_node.children:
            if child.type == "string":
                source_text = child.text.decode("utf-8").strip("'\"")
                source_module = self._resolve_js_module_path(
                    source_text, current_module
                )
                break

        if not source_module:
            return

        # Parse export clause to extract re-exported names
        for child in export_node.children:
            if child.type == "export_clause":
                # Handle named re-exports: export { name1, name2 } from './module'
                for grandchild in child.children:
                    if grandchild.type == "export_specifier":
                        name_node = grandchild.child_by_field_name("name")
                        alias_node = grandchild.child_by_field_name("alias")
                        if name_node and name_node.text:
                            original_name = name_node.text.decode("utf-8")
                            exported_name = (
                                alias_node.text.decode("utf-8")
                                if alias_node and alias_node.text
                                else original_name
                            )
                            self.import_mapping[current_module][exported_name] = (
                                f"{source_module}.{original_name}"
                            )
                            logger.debug(
                                f"JS re-export: {exported_name} -> "
                                f"{source_module}.{original_name}"
                            )
            elif child.type == "*":
                # Handle namespace re-exports: export * from './module'
                wildcard_key = f"*{source_module}"
                self.import_mapping[current_module][wildcard_key] = source_module
                logger.debug(f"JS namespace re-export: * -> {source_module}")

    def _parse_java_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Java import statements."""

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
                    logger.debug(f"Java wildcard import: {imported_path}.*")
                    # Store wildcard import for potential future use
                    self.import_mapping[module_qn][f"*{imported_path}"] = imported_path
                else:
                    # import java.util.List; or import static java.lang.Math.PI;
                    parts = imported_path.split(".")
                    if parts:
                        imported_name = parts[-1]  # Last part is class/method name
                        if is_static:
                            # Static import - method/field can be used directly
                            self.import_mapping[module_qn][imported_name] = (
                                imported_path
                            )
                            logger.debug(
                                f"Java static import: {imported_name} -> "
                                f"{imported_path}"
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

    def _parse_cpp_imports(self, captures: dict, module_qn: str) -> None:
        """Parse C++ #include statements and C++20 module imports."""
        # Parse traditional #include statements
        for import_node in captures.get("import", []):
            if import_node.type == "preproc_include":
                self._parse_cpp_include(import_node, module_qn)
            elif import_node.type == "template_function":
                # Handle "import <header>;" syntax
                self._parse_cpp_module_import(import_node, module_qn)
            elif import_node.type == "declaration":
                # Handle "module math_operations;" declarations and "export import :partition;"
                self._parse_cpp_module_declaration(import_node, module_qn)

    def _parse_cpp_include(self, include_node: Node, module_qn: str) -> None:
        """Parse a single C++ #include statement."""
        include_path = None
        is_system_include = False

        for child in include_node.children:
            if child.type == "string_literal":
                # Local include: #include "header.h"
                include_path = child.text.decode("utf-8").strip('"')
                is_system_include = False
            elif child.type == "system_lib_string":
                # System include: #include <iostream>
                include_path = child.text.decode("utf-8").strip("<>")
                is_system_include = True

        if include_path:
            # Extract the header name for the local mapping
            header_name = include_path.split("/")[-1]
            if header_name.endswith(".h") or header_name.endswith(".hpp"):
                local_name = header_name.split(".")[0]
            else:
                local_name = header_name

            # Build full qualified name
            if is_system_include:
                # System includes map to external libraries
                full_name = (
                    f"std.{include_path}"
                    if not include_path.startswith("std")
                    else include_path
                )
            else:
                # Local includes map to project modules
                # Convert path/to/header.h to project.path.to.header
                path_parts = (
                    include_path.replace("/", ".").replace(".h", "").replace(".hpp", "")
                )
                full_name = f"{self.project_name}.{path_parts}"

            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(
                f"C++ include: {local_name} -> {full_name} (system: {is_system_include})"
            )

    def _parse_cpp_module_import(self, import_node: Node, module_qn: str) -> None:
        """Parse C++20 module import statements like 'import <iostream>;'."""
        # Check if this is actually an import statement
        identifier_child = None
        template_args_child = None

        for child in import_node.children:
            if child.type == "identifier":
                identifier_child = child
            elif child.type == "template_argument_list":
                template_args_child = child

        # Only process if the identifier is "import"
        if identifier_child and identifier_child.text.decode("utf-8") == "import":
            if template_args_child:
                # Extract the module/header name from <...>
                module_name = None
                for child in template_args_child.children:
                    if child.type == "type_descriptor":
                        for desc_child in child.children:
                            if desc_child.type == "type_identifier":
                                module_name = desc_child.text.decode("utf-8")
                                break
                    elif child.type == "type_identifier":
                        module_name = child.text.decode("utf-8")

                if module_name:
                    # This is a standard library module import like "import <iostream>;"
                    local_name = module_name
                    full_name = f"std.{module_name}"

                    self.import_mapping[module_qn][local_name] = full_name
                    logger.debug(f"C++20 module import: {local_name} -> {full_name}")

    def _parse_cpp_module_declaration(self, decl_node: Node, module_qn: str) -> None:
        """Parse C++20 module declarations and partition imports."""
        # Extract text to analyze the declaration
        decl_text = decl_node.text.decode("utf-8").strip()

        if decl_text.startswith("module ") and not decl_text.startswith("module ;"):
            # Parse "module math_operations;" - this is a module implementation file
            parts = decl_text.split()
            if len(parts) >= 2:
                module_name = parts[1].rstrip(";")
                # Record that this file implements the specified module
                self.import_mapping[module_qn][module_name] = (
                    f"{self.project_name}.{module_name}"
                )
                logger.debug(f"C++20 module implementation: {module_name}")

        elif decl_text.startswith("export module "):
            # Parse "export module math_operations;" - this is a module interface
            parts = decl_text.split()
            if len(parts) >= 3:
                module_name = parts[2].rstrip(";")
                # Record that this file exports the specified module
                self.import_mapping[module_qn][module_name] = (
                    f"{self.project_name}.{module_name}"
                )
                logger.debug(f"C++20 module interface: {module_name}")

        elif "import :" in decl_text:
            # Parse "export import :containers;" - this is a partition import
            if (
                ":containers" in decl_text
                or ":algorithms" in decl_text
                or ":iterators" in decl_text
            ):
                # Extract partition name
                colon_pos = decl_text.find(":")
                if colon_pos != -1:
                    partition_part = decl_text[colon_pos + 1 :].split(";")[0].strip()
                    # Create mapping for the partition
                    partition_name = f"partition_{partition_part}"
                    full_name = f"{self.project_name}.{partition_part}"

                    self.import_mapping[module_qn][partition_name] = full_name
                    logger.debug(
                        f"C++20 module partition import: {partition_name} -> {full_name}"
                    )

    def _parse_generic_imports(
        self, captures: dict, module_qn: str, lang_config: LanguageConfig
    ) -> None:
        """Generic fallback import parsing for other languages."""

        for import_node in captures.get("import", []):
            logger.debug(
                f"Generic import parsing for {lang_config.name}: {import_node.type}"
            )
