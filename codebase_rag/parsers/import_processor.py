"""Import processor for parsing and resolving import statements."""

from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..language_config import LanguageConfig
from .lua_utils import (
    extract_lua_assigned_name,
    extract_lua_pcall_second_identifier,
)
from .utils import safe_decode_text, safe_decode_with_fallback

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
            elif language == "lua":
                self._parse_lua_imports(captures, module_qn)
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
                module_name = safe_decode_text(child) or ""
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

                if module_name_node and alias_node:
                    decoded_module_name = safe_decode_text(module_name_node)
                    decoded_alias = safe_decode_text(alias_node)
                    if not decoded_module_name or not decoded_alias:
                        continue
                    module_name = decoded_module_name
                    alias = decoded_alias

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
            decoded_name = safe_decode_text(module_name_node)
            if not decoded_name:
                return
            module_name = decoded_name
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
                decoded_name = safe_decode_text(name_node)
                if not decoded_name:
                    continue
                name = decoded_name
                imported_items.append((name, name))
            elif name_node.type == "aliased_import":
                # Aliased import: from module import name as alias
                original_name_node = name_node.child_by_field_name("name")
                alias_node = name_node.child_by_field_name("alias")
                if original_name_node and alias_node:
                    original_name = safe_decode_text(original_name_node)
                    alias = safe_decode_text(alias_node)
                    if not original_name or not alias:
                        continue
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
                decoded_text = safe_decode_text(child)
                if not decoded_text:
                    continue
                dots = len(decoded_text)
            elif child.type == "dotted_name":
                decoded_name = safe_decode_text(child)
                if not decoded_name:
                    continue
                module_name = decoded_name

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
                        source_text = safe_decode_with_fallback(child).strip("'\"")
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
                imported_name = safe_decode_with_fallback(child)
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
                        if name_node:
                            imported_name = safe_decode_with_fallback(name_node)
                            local_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
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
                        namespace_name = safe_decode_with_fallback(grandchild)
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
                        and safe_decode_text(func_node) == "require"
                    ):
                        # Extract module path from first argument
                        for arg in args_node.children:
                            if arg.type == "string":
                                var_name = safe_decode_with_fallback(name_node)
                                required_module = safe_decode_with_fallback(arg).strip(
                                    "'\""
                                )

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
                source_text = safe_decode_with_fallback(child).strip("'\"")
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
                        if name_node:
                            original_name = safe_decode_with_fallback(name_node)
                            exported_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
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
                        imported_path = safe_decode_with_fallback(child)
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
        """Parse a single Rust use declaration using tree-sitter field access."""
        from .rust_utils import extract_rust_use_imports

        # Use the improved tree-sitter-based function to extract imports
        imports = extract_rust_use_imports(use_node)

        # Add all extracted imports to the import mapping
        for imported_name, full_path in imports.items():
            self.import_mapping[module_qn][imported_name] = full_path
            logger.debug(f"Rust import: {imported_name} -> {full_path}")

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
                alias_name = safe_decode_with_fallback(child)
            elif child.type == "interpreted_string_literal":
                # Extract import path from string literal
                import_path = safe_decode_with_fallback(child).strip('"')

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
                include_path = safe_decode_with_fallback(child).strip('"')
                is_system_include = False
            elif child.type == "system_lib_string":
                # System include: #include <iostream>
                include_path = safe_decode_with_fallback(child).strip("<>")
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
        if identifier_child and safe_decode_text(identifier_child) == "import":
            if template_args_child:
                # Extract the module/header name from <...>
                module_name = None
                for child in template_args_child.children:
                    if child.type == "type_descriptor":
                        for desc_child in child.children:
                            if desc_child.type == "type_identifier":
                                module_name = safe_decode_with_fallback(desc_child)
                                break
                    elif child.type == "type_identifier":
                        module_name = safe_decode_with_fallback(child)

                if module_name:
                    # This is a standard library module import like "import <iostream>;"
                    local_name = module_name
                    full_name = f"std.{module_name}"

                    self.import_mapping[module_qn][local_name] = full_name
                    logger.debug(f"C++20 module import: {local_name} -> {full_name}")

    def _parse_cpp_module_declaration(self, decl_node: Node, module_qn: str) -> None:
        """Parse C++20 module declarations and partition imports."""
        # Extract text to analyze the declaration
        decoded_text = safe_decode_text(decl_node)
        if not decoded_text:
            return
        decl_text = decoded_text.strip()

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
            # Parse "export import :partition_name;" - this is a partition import
            # Extract partition name
            colon_pos = decl_text.find(":")
            if colon_pos != -1:
                partition_part = decl_text[colon_pos + 1 :].split(";")[0].strip()
                if partition_part:
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

    # ============================= Lua support ==============================
    def _parse_lua_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Lua require-based imports from function_call captures."""
        for call_node in captures.get("import", []):
            # Check for regular require() calls
            if self._lua_is_require_call(call_node):
                module_path = self._lua_extract_require_arg(call_node)
                if module_path:
                    local_name = (
                        self._lua_extract_assignment_lhs(call_node)
                        or module_path.split(".")[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved
            # Check for pcall(require, 'module') pattern
            elif self._lua_is_pcall_require(call_node):
                module_path = self._lua_extract_pcall_require_arg(call_node)
                if module_path:
                    # For pcall, get the second variable in assignment (first is ok/err)
                    local_name = (
                        self._lua_extract_pcall_assignment_lhs(call_node)
                        or module_path.split(".")[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved

    def _lua_is_require_call(self, call_node: Node) -> bool:
        """Return True if function_call represents require(...) or require 'x'."""
        # In Lua tree-sitter, function calls have the function name as the first child
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == "identifier":
            return safe_decode_text(first_child) == "require"
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
        """Return True if function_call represents pcall(require, 'module')."""
        # Check if first child is 'pcall'
        first_child = call_node.children[0] if call_node.children else None
        if not (
            first_child
            and first_child.type == "identifier"
            and safe_decode_text(first_child) == "pcall"
        ):
            return False

        # Check if first argument is 'require' identifier
        args = call_node.child_by_field_name("arguments")
        if not args:
            return False

        # Find the first expression node in the arguments
        first_arg_node = next(
            (child for child in args.children if child.type not in ["(", ")", ","]),
            None,
        )

        return (
            first_arg_node is not None
            and first_arg_node.type == "identifier"
            and safe_decode_text(first_arg_node) == "require"
        )

    def _lua_extract_require_arg(self, call_node: Node) -> str | None:
        """Extract first string-like argument from a require call."""
        # Look under arguments node if present
        args = call_node.child_by_field_name("arguments")
        candidates = []
        if args:
            candidates.extend(args.children)
        else:
            candidates.extend(call_node.children)
        for node in candidates:
            if node.type in ("string", "string_literal"):
                decoded = safe_decode_text(node)
                if decoded:
                    return decoded.strip("'\"")
        return None

    def _lua_extract_pcall_require_arg(self, call_node: Node) -> str | None:
        """Extract module path from pcall(require, 'module') pattern."""
        args = call_node.child_by_field_name("arguments")
        if not args:
            return None
        # Look for string after 'require' identifier
        found_require = False
        for child in args.children:
            if found_require and child.type in ("string", "string_literal"):
                decoded = safe_decode_text(child)
                if decoded:
                    return decoded.strip("'\"")
            if child.type == "identifier":
                if safe_decode_text(child) == "require":
                    found_require = True
        return None

    def _lua_extract_assignment_lhs(self, call_node: Node) -> str | None:
        """Find identifier assigned from the require call (local or global)."""
        # Use shared utility to extract the assigned name (only identifiers for require)
        return extract_lua_assigned_name(call_node, accepted_var_types=("identifier",))

    def _lua_extract_pcall_assignment_lhs(self, call_node: Node) -> str | None:
        """Find the second identifier assigned from pcall(require, ...) pattern.

        In patterns like: local ok, json = pcall(require, 'json')
        We want to extract 'json' (the second identifier).
        """
        # Use shared utility to extract the second identifier from pcall pattern
        return extract_lua_pcall_second_identifier(call_node)

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        """Resolve Lua module path for require. Handles ./ and ../ prefixes."""
        if import_path.startswith("./") or import_path.startswith("../"):
            parts = current_module.split(".")[:-1]
            rel_parts = [p for p in import_path.replace("\\", "/").split("/")]
            for p in rel_parts:
                if p == ".":
                    continue
                if p == "..":
                    if parts:
                        parts.pop()
                elif p:
                    parts.append(p)
            return ".".join(parts)
        # Dotted or bare names: determine if they exist locally to prefix with project
        # Convert any remaining path separators to dots
        dotted = import_path.replace("/", ".")

        # Try to detect local file presence
        try:
            # For dotted path like pkg.mod -> pkg/mod.lua
            relative_file = dotted.replace(".", "/") + ".lua"
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}.{dotted}"
            # For bare name like mod -> mod.lua
            if (self.repo_path / f"{dotted}.lua").is_file():
                return f"{self.project_name}.{dotted}"
        except OSError:
            pass

        return dotted
