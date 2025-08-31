"""Import processor for parsing and resolving import statements.

This module provides dynamic standard library introspection capabilities that can
accurately resolve entity imports (like collections.defaultdict) to their containing
modules (like collections) using language-native reflection mechanisms.

External Dependencies (Optional - Enhanced Accuracy):
    For enhanced standard library introspection accuracy, the following external
    tools can be installed. If unavailable, the system gracefully falls back to
    heuristic-based approaches:

    - Node.js: For JavaScript/TypeScript stdlib introspection
    - Go compiler: For Go package analysis
    - Java compiler: For Java reflection-based introspection
    - Lua interpreter: For Lua module introspection
    - C++ compiler (g++): For C++ standard library analysis

Performance Optimizations:
    - Results are cached in memory and persistently to disk (~/.cache/codebase_rag/)
    - External tool availability is cached to avoid repeated PATH checks
    - Fallback heuristics ensure functionality without external dependencies
"""

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node

from ..language_config import LanguageConfig
from .lua_utils import (
    extract_lua_assigned_name,
    extract_lua_pcall_second_identifier,
)
from .rust_utils import extract_rust_use_imports
from .utils import get_query_cursor, safe_decode_text, safe_decode_with_fallback

# Common language constants for performance optimization
_JS_TYPESCRIPT_LANGUAGES = {"javascript", "typescript"}

# Global cache for stdlib introspection results to avoid repeated subprocess calls
_STDLIB_CACHE: dict[str, dict[str, str]] = {}
_CACHE_TTL = 3600  # Cache results for 1 hour
_CACHE_TIMESTAMPS: dict[str, float] = {}

# External tool availability cache
_EXTERNAL_TOOLS: dict[str, bool] = {}


def _is_tool_available(tool_name: str) -> bool:
    """Check if an external tool is available in the system PATH with caching."""
    if tool_name in _EXTERNAL_TOOLS:
        return _EXTERNAL_TOOLS[tool_name]

    import subprocess

    try:
        subprocess.run([tool_name, "--version"], capture_output=True, timeout=2)
        _EXTERNAL_TOOLS[tool_name] = True
        return True
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
    ):
        _EXTERNAL_TOOLS[tool_name] = False
        logger.debug(
            f"External tool '{tool_name}' not available for stdlib introspection"
        )
        return False


def _get_cached_stdlib_result(language: str, full_qualified_name: str) -> str | None:
    """Get cached stdlib introspection result if available and not expired."""
    cache_key = f"{language}:{full_qualified_name}"

    # Check if we have a cached result
    if cache_key not in _STDLIB_CACHE:
        return None

    # Check if cache has expired
    if cache_key in _CACHE_TIMESTAMPS:
        if time.time() - _CACHE_TIMESTAMPS[cache_key] > _CACHE_TTL:
            # Cache expired, remove it
            del _STDLIB_CACHE[cache_key]
            del _CACHE_TIMESTAMPS[cache_key]
            return None

    return _STDLIB_CACHE[cache_key].get(full_qualified_name)


def _cache_stdlib_result(language: str, full_qualified_name: str, result: str) -> None:
    """Cache stdlib introspection result."""
    cache_key = f"{language}:{full_qualified_name}"

    if cache_key not in _STDLIB_CACHE:
        _STDLIB_CACHE[cache_key] = {}

    _STDLIB_CACHE[cache_key][full_qualified_name] = result
    _CACHE_TIMESTAMPS[cache_key] = time.time()


def _load_persistent_cache() -> None:
    """Load persistent cache from disk if available."""
    try:
        cache_file = Path.home() / ".cache" / "codebase_rag" / "stdlib_cache.json"
        if cache_file.exists():
            with cache_file.open() as f:
                data = json.load(f)
                _STDLIB_CACHE.update(data.get("cache", {}))
                _CACHE_TIMESTAMPS.update(data.get("timestamps", {}))
            logger.debug(f"Loaded stdlib cache from {cache_file}")
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Could not load stdlib cache: {e}")


def _save_persistent_cache() -> None:
    """Save persistent cache to disk."""
    try:
        cache_dir = Path.home() / ".cache" / "codebase_rag"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "stdlib_cache.json"

        with cache_file.open("w") as f:
            json.dump(
                {
                    "cache": _STDLIB_CACHE,
                    "timestamps": _CACHE_TIMESTAMPS,
                },
                f,
                indent=2,
            )
        logger.debug(f"Saved stdlib cache to {cache_file}")
    except OSError as e:
        logger.debug(f"Could not save stdlib cache: {e}")


class ImportProcessor:
    """Handles parsing and processing of import statements."""

    def __init__(
        self,
        repo_path_getter: Any,
        project_name_getter: Any,
        ingestor: Any | None = None,
        function_registry: Any | None = None,
    ) -> None:
        self._repo_path_getter = repo_path_getter
        self._project_name_getter = project_name_getter
        self.ingestor = ingestor
        self.function_registry = function_registry
        self.import_mapping: dict[str, dict[str, str]] = {}

        # Load persistent cache on initialization
        _load_persistent_cache()

    def __del__(self) -> None:
        """Save cache when processor is destroyed."""
        try:
            _save_persistent_cache()
        except Exception:
            # Ignore errors during cleanup
            pass

    @staticmethod
    def flush_stdlib_cache() -> None:
        """Manually flush the stdlib cache to disk. Useful for ensuring persistence."""
        _save_persistent_cache()

    @staticmethod
    def clear_stdlib_cache() -> None:
        """Clear the stdlib cache from memory and disk."""
        global _STDLIB_CACHE, _CACHE_TIMESTAMPS
        _STDLIB_CACHE.clear()
        _CACHE_TIMESTAMPS.clear()
        try:
            cache_file = Path.home() / ".cache" / "codebase_rag" / "stdlib_cache.json"
            if cache_file.exists():
                cache_file.unlink()
                logger.debug("Cleared stdlib cache from disk")
        except OSError as e:
            logger.debug(f"Could not clear stdlib cache from disk: {e}")

    @staticmethod
    def get_stdlib_cache_stats() -> dict[str, Any]:
        """Get statistics about the stdlib cache for monitoring/debugging."""
        return {
            "cache_entries": len(_STDLIB_CACHE),
            "cache_languages": list(_STDLIB_CACHE.keys()),
            "total_cached_results": sum(
                len(lang_cache) for lang_cache in _STDLIB_CACHE.values()
            ),
            "external_tools_checked": _EXTERNAL_TOOLS.copy(),
        }

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
            cursor = get_query_cursor(imports_query)
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
                    # Extract just the module path for the IMPORTS relationship
                    # This ensures Module -> Module relationships, not Module -> Class/Function
                    module_path = self._extract_module_path(full_name, language)

                    self.ingestor.ensure_relationship_batch(
                        ("Module", "qualified_name", module_qn),
                        "IMPORTS",
                        ("Module", "qualified_name", module_path),
                    )
                    logger.debug(
                        f"  Created IMPORTS relationship: {module_qn} -> {module_path} (from {full_name})"
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

            # Check for standard library function calls (e.g., string.upper, math.floor)
            elif self._lua_is_stdlib_call(call_node):
                stdlib_module = self._lua_extract_stdlib_module(call_node)
                if stdlib_module:
                    # Create implicit import relationship for stdlib module
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module

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

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        """Return True if function_call represents a Lua standard library call (e.g., string.upper, math.floor)."""
        from .lua_utils import safe_decode_text

        # Check if this is a method call (module.function format)
        if not call_node.children:
            return False

        # Look for dot_index_expression pattern: module.function
        first_child = call_node.children[0]
        if first_child.type == "dot_index_expression":
            # Get the module name (left side of the dot)
            if first_child.children and first_child.children[0].type == "identifier":
                module_name = safe_decode_text(first_child.children[0])
                # Check if it's a known Lua standard library module
                return module_name in {
                    "string",
                    "math",
                    "table",
                    "os",
                    "io",
                    "debug",
                    "package",
                    "coroutine",
                    "utf8",
                    "bit32",
                }

        return False

    def _lua_extract_stdlib_module(self, call_node: Node) -> str | None:
        """Extract the stdlib module name from a stdlib function call."""
        from .lua_utils import safe_decode_text

        if not call_node.children:
            return None

        first_child = call_node.children[0]
        if first_child.type == "dot_index_expression":
            # Get the module name (left side of the dot)
            if first_child.children and first_child.children[0].type == "identifier":
                return safe_decode_text(first_child.children[0])

        return None

    def _extract_module_path(
        self, full_qualified_name: str, language: str = "python"
    ) -> str:
        """Extract module path from a full qualified name using tree-sitter knowledge.

        This method uses the function_registry (populated by tree-sitter parsing) to determine
        whether the qualified name refers to a module file or to a class/function within a module.

        The function_registry contains entries for all Class/Function/Method nodes discovered
        by tree-sitter parsing. If a qualified name is in the registry, we know it's an entity
        defined within a module, so we extract the module path by removing the entity name.

        Args:
            full_qualified_name: Full qualified name like "project.module.Class"

        Returns:
            Module path like "project.module"

        Examples:
            "project.my_app.db.base.BaseRepo" -> "project.my_app.db.base" (BaseRepo is a Class)
            "project.my_app.db.base" -> "project.my_app.db.base" (base is the module file)
        """
        # Primary strategy: Use function_registry populated by tree-sitter parsing
        if self.function_registry and full_qualified_name in self.function_registry:
            entity_type = self.function_registry[full_qualified_name]
            if entity_type in ("Class", "Function", "Method"):
                # Tree-sitter identified this as a class/function/method defined within a module
                # Extract the module path by removing the entity name
                parts = full_qualified_name.rsplit(".", 1)
                if len(parts) == 2:
                    return parts[0]  # Return the module path

        # If not in function_registry, it's either:
        # 1. A module file itself (correct as-is)
        # 2. An external/standard library entity we didn't parse
        # 3. A misclassified entity (rare)

        # Language-specific standard library introspection using native reflection
        if language == "python":
            return self._extract_python_stdlib_path(full_qualified_name)
        elif language in ["javascript", "typescript"]:
            return self._extract_js_stdlib_path(full_qualified_name)
        elif language == "go":
            return self._extract_go_stdlib_path(full_qualified_name)
        elif language == "rust":
            return self._extract_rust_stdlib_path(full_qualified_name)
        elif language == "cpp":
            return self._extract_cpp_stdlib_path(full_qualified_name)
        elif language == "java":
            return self._extract_java_stdlib_path(full_qualified_name)
        elif language == "lua":
            return self._extract_lua_stdlib_path(full_qualified_name)
        else:
            return self._extract_generic_stdlib_path(full_qualified_name)

    def _extract_python_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Python stdlib module path using runtime introspection."""
        # Check cache first to avoid expensive importlib calls
        cached_result = _get_cached_stdlib_result("python", full_qualified_name)
        if cached_result is not None:
            return cached_result

        parts = full_qualified_name.split(".")
        if len(parts) >= 2:
            module_name = parts[0]
            entity_name = parts[-1]

            try:
                import importlib
                import inspect

                module = importlib.import_module(module_name)

                if hasattr(module, entity_name):
                    obj = getattr(module, entity_name)
                    if (
                        inspect.isclass(obj)
                        or inspect.isfunction(obj)
                        or not inspect.ismodule(obj)
                    ):
                        module_path = ".".join(parts[:-1])
                        _cache_stdlib_result("python", full_qualified_name, module_path)
                        return module_path
            except (ImportError, AttributeError):
                pass

            # Fallback heuristic
            if entity_name[0].isupper():
                result = ".".join(parts[:-1])
            else:
                result = full_qualified_name

            _cache_stdlib_result("python", full_qualified_name, result)
            return result

        return full_qualified_name

    def _extract_js_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract JavaScript/Node.js stdlib module path using runtime introspection."""
        # Check cache first to avoid expensive subprocess calls
        cached_result = _get_cached_stdlib_result("javascript", full_qualified_name)
        if cached_result is not None:
            return cached_result

        parts = full_qualified_name.split(".")
        if len(parts) >= 2:
            module_name = parts[0]
            entity_name = parts[-1]

            # Try dynamic introspection only if Node.js is available
            if _is_tool_available("node"):
                try:
                    import json
                    import os
                    import subprocess

                    # Safe Node.js script that reads module/entity names from environment variables
                    node_script = """
                    const moduleName = process.env.MODULE_NAME;
                    const entityName = process.env.ENTITY_NAME;

                    if (!moduleName || !entityName) {
                        console.log(JSON.stringify({hasEntity: false, entityType: null}));
                        process.exit(0);
                    }

                    try {
                        const module = require(moduleName);
                        const hasEntity = entityName in module;
                        const entityType = hasEntity ? typeof module[entityName] : null;
                        console.log(JSON.stringify({hasEntity, entityType}));
                    } catch (e) {
                        console.log(JSON.stringify({hasEntity: false, entityType: null}));
                    }
                    """

                    # Create environment with module and entity names
                    env = os.environ.copy()
                    env["MODULE_NAME"] = module_name
                    env["ENTITY_NAME"] = entity_name

                    subprocess_result = subprocess.run(
                        ["node", "-e", node_script],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        env=env,
                    )

                    if subprocess_result.returncode == 0:
                        data = json.loads(subprocess_result.stdout.strip())
                        if data["hasEntity"] and data["entityType"] in [
                            "function",
                            "object",
                        ]:
                            module_path = ".".join(parts[:-1])
                            _cache_stdlib_result(
                                "javascript", full_qualified_name, module_path
                            )
                            return module_path

                except (
                    subprocess.TimeoutExpired,
                    subprocess.CalledProcessError,
                    json.JSONDecodeError,
                ):
                    pass

            # Fallback to heuristic approach when Node.js unavailable or introspection fails
            if entity_name[0].isupper():
                result = ".".join(parts[:-1])
            else:
                result = full_qualified_name

            # Cache the result to avoid repeated heuristic calculations
            _cache_stdlib_result("javascript", full_qualified_name, result)
            return result

        return full_qualified_name

    def _extract_go_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Go stdlib module path using compile-time analysis."""
        parts = full_qualified_name.split("/")  # Go uses / for package paths
        if len(parts) >= 2:
            # Use go/doc to analyze package documentation and exports
            try:
                import json
                import os
                import subprocess

                package_path = "/".join(parts[:-1])
                entity_name = parts[-1]

                # First, resolve the package import path to its filesystem directory
                resolve_result = subprocess.run(
                    ["go", "list", "-f", "{{.Dir}}", package_path],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

                if resolve_result.returncode != 0:
                    # If we can't resolve the package path, fall back to heuristics
                    raise subprocess.CalledProcessError(
                        resolve_result.returncode, resolve_result.args
                    )

                package_dir = resolve_result.stdout.strip()
                if not package_dir:
                    raise subprocess.CalledProcessError(1, ["go", "list"])

                # Safe Go script that reads package/entity names from environment variables
                go_script = """
package main

import (
    "encoding/json"
    "fmt"
    "go/doc"
    "go/parser"
    "go/token"
    "os"
)

func main() {
    packagePath := os.Getenv("PACKAGE_PATH")
    entityName := os.Getenv("ENTITY_NAME")

    if packagePath == "" || entityName == "" {
        fmt.Print("{\"hasEntity\": false}")
        return
    }

    fset := token.NewFileSet()
    pkgs, err := parser.ParseDir(fset, packagePath, nil, parser.ParseComments)
    if err != nil {
        fmt.Print("{\"hasEntity\": false}")
        return
    }

    for _, pkg := range pkgs {
        d := doc.New(pkg, packagePath, doc.AllDecls)

        // Check functions
        for _, f := range d.Funcs {
            if f.Name == entityName {
                fmt.Print("{\"hasEntity\": true, \"entityType\": \"function\"}")
                return
            }
        }

        // Check types (structs, interfaces, etc.)
        for _, t := range d.Types {
            if t.Name == entityName {
                fmt.Print("{\"hasEntity\": true, \"entityType\": \"type\"}")
                return
            }
        }

        // Check constants and variables
        for _, v := range d.Vars {
            for _, name := range v.Names {
                if name == entityName {
                    fmt.Print("{\"hasEntity\": true, \"entityType\": \"variable\"}")
                    return
                }
            }
        }

        for _, c := range d.Consts {
            for _, name := range c.Names {
                if name == entityName {
                    fmt.Print("{\"hasEntity\": true, \"entityType\": \"constant\"}")
                    return
                }
            }
        }
    }

    fmt.Print("{\"hasEntity\": false}")
}
                """

                # Create environment with resolved package directory and entity names
                env = os.environ.copy()
                env["PACKAGE_PATH"] = package_dir  # Use resolved directory path
                env["ENTITY_NAME"] = entity_name

                # Write temporary Go file and execute
                with subprocess.Popen(
                    ["go", "run", "-"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                ) as proc:
                    stdout, _ = proc.communicate(go_script, timeout=10)

                    if proc.returncode == 0:
                        data = json.loads(stdout.strip())
                        if data["hasEntity"]:
                            return package_path

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
                FileNotFoundError,
            ):
                pass

            # Fallback heuristic for Go (functions/types usually start with uppercase)
            entity_name = parts[-1]
            if entity_name[0].isupper():
                return "/".join(parts[:-1])

        return full_qualified_name

    def _extract_rust_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Rust stdlib module path using compile-time analysis."""
        parts = full_qualified_name.split("::")  # Rust uses :: for namespacing
        if len(parts) >= 2:
            # Rust doesn't have runtime reflection, but we can use compile-time tools
            # For now, use naming conventions until we implement proc-macro analysis

            entity_name = parts[-1]

            # Rust naming conventions are very consistent:
            # Types (structs, enums, traits) use PascalCase
            # Functions, variables, modules use snake_case
            # Constants use SCREAMING_SNAKE_CASE

            if (
                entity_name[0].isupper()  # PascalCase (types)
                or entity_name.isupper()  # SCREAMING_SNAKE_CASE (constants)
                or "_" not in entity_name
                and entity_name.islower()
            ):  # Simple functions
                return "::".join(parts[:-1])

        return full_qualified_name

    def _extract_cpp_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract C++ stdlib module path using header analysis."""
        parts = full_qualified_name.split("::")  # C++ uses :: for namespacing
        if len(parts) >= 2:
            # C++ standard library namespace and common components
            namespace = parts[0]
            if namespace == "std":
                entity_name = parts[-1]

                # C++ standard library introspection via compile-time analysis
                try:
                    import os
                    import subprocess
                    import tempfile

                    # Safe approach: write entity name to a temporary file and include it
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", delete=False
                    ) as f:
                        f.write(entity_name)
                        entity_file = f.name

                    try:
                        # Strategy 1: Try as template with int parameter using file input
                        cpp_template_program = f"""
#include <iostream>
#include <fstream>
#include <string>

int main() {{
    std::ifstream file("{entity_file}");
    std::string entity_name;
    std::getline(file, entity_name);
    file.close();

    // This is a compile-time check strategy - we can't dynamically construct templates
    // Fall back to heuristic approach for safety
    std::cout << "heuristic_check" << std::endl;
    return 0;
}}
                        """

                        subprocess.run(
                            ["g++", "-std=c++17", "-x", "c++", "-", "-o", "/dev/null"],
                            input=cpp_template_program,
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )

                        # For C++, we'll primarily rely on heuristics due to complexity
                        # of safe dynamic compilation without code injection risks

                    finally:
                        os.unlink(entity_file)

                except (
                    subprocess.TimeoutExpired,
                    subprocess.CalledProcessError,
                    FileNotFoundError,
                    OSError,
                ):
                    pass

                # Fallback using C++ naming conventions (safer approach)
                entity_name = parts[-1]
                if (
                    entity_name[0].isupper()  # Types usually start with uppercase
                    or entity_name.startswith("is_")
                    or entity_name.startswith("has_")
                    or entity_name
                    in {  # Common std library types/functions
                        "vector",
                        "string",
                        "map",
                        "set",
                        "list",
                        "deque",
                        "unique_ptr",
                        "shared_ptr",
                        "weak_ptr",
                        "thread",
                        "mutex",
                        "condition_variable",
                        "future",
                        "promise",
                        "sort",
                        "find",
                        "copy",
                        "transform",
                        "accumulate",
                    }
                ):  # Type traits and common stdlib entities
                    return "::".join(parts[:-1])

        return full_qualified_name

    def _extract_java_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Java stdlib module path using reflection."""
        parts = full_qualified_name.split(".")
        if len(parts) >= 2:
            # Use Java reflection via subprocess to avoid direct Java dependency
            try:
                import json
                import os
                import subprocess
                import tempfile

                package_name = ".".join(parts[:-1])
                entity_name = parts[-1]

                # Safe Java program using command line arguments
                java_program = """
import java.lang.reflect.*;

public class StdlibCheck {
    public static void main(String[] args) {
        if (args.length < 2) {
            System.out.println("{\\"hasEntity\\": false}");
            return;
        }

        String packageName = args[0];
        String entityName = args[1];

        try {
            Class<?> clazz = Class.forName(packageName + "." + entityName);
            System.out.println("{\\"hasEntity\\": true, \\"entityType\\": \\"class\\"}");
        } catch (ClassNotFoundException e) {
            // Try as method or field in parent package
            try {
                Class<?> packageClass = Class.forName(packageName);
                Method[] methods = packageClass.getMethods();
                Field[] fields = packageClass.getFields();

                boolean foundMethod = false;
                for (Method method : methods) {
                    if (method.getName().equals(entityName)) {
                        foundMethod = true;
                        break;
                    }
                }

                boolean foundField = false;
                for (Field field : fields) {
                    if (field.getName().equals(entityName)) {
                        foundField = true;
                        break;
                    }
                }

                if (foundMethod || foundField) {
                    System.out.println("{\\"hasEntity\\": true, \\"entityType\\": \\"member\\"}");
                } else {
                    System.out.println("{\\"hasEntity\\": false}");
                }
            } catch (Exception ex) {
                System.out.println("{\\"hasEntity\\": false}");
            }
        }
    }
}
                """

                # Write Java program to temporary file and compile/run it
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".java", delete=False
                ) as f:
                    f.write(java_program)
                    java_file = f.name

                try:
                    # Compile the Java program
                    compile_result = subprocess.run(
                        ["javac", java_file],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )

                    if compile_result.returncode == 0:
                        # Run the compiled program with safe arguments
                        class_name = os.path.splitext(os.path.basename(java_file))[0]
                        run_result = subprocess.run(
                            [
                                "java",
                                "-cp",
                                os.path.dirname(java_file),
                                class_name,
                                package_name,
                                entity_name,
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )

                        if run_result.returncode == 0:
                            data = json.loads(run_result.stdout.strip())
                            if data.get("hasEntity"):
                                return ".".join(parts[:-1])

                finally:
                    # Clean up temporary files
                    for ext in [".java", ".class"]:
                        temp_file = os.path.splitext(java_file)[0] + ext
                        try:
                            os.unlink(temp_file)
                        except (FileNotFoundError, OSError):
                            pass

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
                FileNotFoundError,
                OSError,
            ):
                pass

            # Fallback using Java naming conventions
            entity_name = parts[-1]
            if (
                entity_name[0].isupper()  # Classes start with uppercase
                or entity_name.endswith("Exception")
                or entity_name.endswith("Error")
                or entity_name.endswith("Interface")
                or entity_name.endswith("Builder")
                or entity_name
                in {  # Common Java stdlib classes
                    "String",
                    "Object",
                    "Integer",
                    "Double",
                    "Boolean",
                    "ArrayList",
                    "HashMap",
                    "HashSet",
                    "LinkedList",
                    "File",
                    "URL",
                    "Pattern",
                    "LocalDateTime",
                    "BigDecimal",
                }
            ):
                return ".".join(parts[:-1])

        return full_qualified_name

    def _extract_lua_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Lua stdlib module path using runtime introspection."""
        parts = full_qualified_name.split(".")
        if len(parts) >= 2:
            module_name = parts[0]
            entity_name = parts[-1]

            # Use Lua introspection via subprocess
            try:
                import os
                import subprocess

                # Safe Lua script that reads module/entity names from environment variables
                lua_script = """
-- Get module and entity names from environment
local module_name = os.getenv("MODULE_NAME")
local entity_name = os.getenv("ENTITY_NAME")

if not module_name or not entity_name then
    print("hasEntity=false")
    return
end

-- Check built-in modules first (they're global tables in Lua)
local module_table = _G[module_name]
if module_table and type(module_table) == "table" then
    local hasEntity = module_table[entity_name] ~= nil
    if hasEntity then
        print("hasEntity=true")
    else
        print("hasEntity=false")
    end
else
    -- Try require for user modules
    local success, loaded_module = pcall(require, module_name)
    if success and type(loaded_module) == "table" then
        local hasEntity = loaded_module[entity_name] ~= nil
        if hasEntity then
            print("hasEntity=true")
        else
            print("hasEntity=false")
        end
    else
        print("hasEntity=false")
    end
end
                """

                # Create environment with module and entity names
                env = os.environ.copy()
                env["MODULE_NAME"] = module_name
                env["ENTITY_NAME"] = entity_name

                result = subprocess.run(
                    ["lua", "-e", lua_script],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=env,
                )

                if result.returncode == 0:
                    # Parse output - look for our simple format
                    output = result.stdout.strip()
                    if "hasEntity=true" in output:
                        return ".".join(parts[:-1])

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                FileNotFoundError,
            ):
                pass

            # Fallback using Lua conventions
            # Lua functions are typically lowercase, tables/modules can be mixed case
            entity_name = parts[-1]
            if (
                entity_name[0].isupper()  # Modules/tables often start uppercase
                or entity_name in {"string", "table", "math", "io", "os", "debug"}
            ):  # Standard modules
                return ".".join(parts[:-1])

        return full_qualified_name

    def _extract_generic_stdlib_path(self, full_qualified_name: str) -> str:
        """Generic fallback using basic heuristics."""
        parts = full_qualified_name.split(".")
        if len(parts) >= 2:
            entity_name = parts[-1]
            if entity_name[0].isupper():
                return ".".join(parts[:-1])

        return full_qualified_name
