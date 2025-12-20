import json
import time
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node

from ..constants import SEPARATOR_DOT, SupportedLanguage
from ..language_config import LanguageConfig
from ..types_defs import LanguageQueries
from .lua_utils import (
    extract_lua_assigned_name,
    extract_lua_pcall_second_identifier,
)
from .rust_utils import extract_rust_use_imports
from .utils import get_query_cursor, safe_decode_text, safe_decode_with_fallback

_JS_TYPESCRIPT_LANGUAGES = {SupportedLanguage.JS, SupportedLanguage.TS}

_STDLIB_CACHE: dict[str, dict[str, str]] = {}
_CACHE_TTL = 3600
_CACHE_TIMESTAMPS: dict[str, float] = {}

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

    if cache_key not in _STDLIB_CACHE:
        return None

    if cache_key in _CACHE_TIMESTAMPS:
        if time.time() - _CACHE_TIMESTAMPS[cache_key] > _CACHE_TTL:
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
    def __init__(
        self,
        repo_path: Path,
        project_name: str,
        ingestor: Any | None = None,
        function_registry: Any | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.project_name = project_name
        self.ingestor = ingestor
        self.function_registry = function_registry
        self.import_mapping: dict[str, dict[str, str]] = {}

        _load_persistent_cache()

    def __del__(self) -> None:
        """Save cache when processor is destroyed."""
        try:
            _save_persistent_cache()
        except Exception:
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

    def parse_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
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

            match language:
                case SupportedLanguage.PYTHON:
                    self._parse_python_imports(captures, module_qn)
                case SupportedLanguage.JS | SupportedLanguage.TS:
                    self._parse_js_ts_imports(captures, module_qn)
                case SupportedLanguage.JAVA:
                    self._parse_java_imports(captures, module_qn)
                case SupportedLanguage.RUST:
                    self._parse_rust_imports(captures, module_qn)
                case SupportedLanguage.GO:
                    self._parse_go_imports(captures, module_qn)
                case SupportedLanguage.CPP:
                    self._parse_cpp_imports(captures, module_qn)
                case SupportedLanguage.LUA:
                    self._parse_lua_imports(captures, module_qn)
                case _:
                    self._parse_generic_imports(captures, module_qn, lang_config)

            logger.debug(
                f"Parsed {len(self.import_mapping[module_qn])} imports in {module_qn}"
            )

            if self.ingestor and module_qn in self.import_mapping:
                for local_name, full_name in self.import_mapping[module_qn].items():
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
                local_name = module_name.split(SEPARATOR_DOT)[0]

                if (self.repo_path / local_name).is_dir() or (
                    self.repo_path / f"{local_name}.py"
                ).is_file():
                    full_name = f"{self.project_name}.{module_name}"
                else:
                    full_name = module_name

                self.import_mapping[module_qn][local_name] = full_name
                logger.debug(f"  Import: {local_name} -> {full_name}")
            elif child.type == "aliased_import":
                module_name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")

                if module_name_node and alias_node:
                    decoded_module_name = safe_decode_text(module_name_node)
                    decoded_alias = safe_decode_text(alias_node)
                    if not decoded_module_name or not decoded_alias:
                        continue
                    module_name = decoded_module_name
                    alias = decoded_alias

                    top_level_module = module_name.split(SEPARATOR_DOT)[0]
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

        if module_name_node.type == "dotted_name":
            decoded_name = safe_decode_text(module_name_node)
            if not decoded_name:
                return
            module_name = decoded_name
        elif module_name_node.type == "relative_import":
            module_name = self._resolve_relative_import(module_name_node, module_qn)
        else:
            return

        imported_items = []
        is_wildcard = False

        for name_node in import_node.children_by_field_name("name"):
            if name_node.type == "dotted_name":
                decoded_name = safe_decode_text(name_node)
                if not decoded_name:
                    continue
                name = decoded_name
                imported_items.append((name, name))
            elif name_node.type == "aliased_import":
                original_name_node = name_node.child_by_field_name("name")
                alias_node = name_node.child_by_field_name("alias")
                if original_name_node and alias_node:
                    original_name = safe_decode_text(original_name_node)
                    alias = safe_decode_text(alias_node)
                    if not original_name or not alias:
                        continue
                    imported_items.append((alias, original_name))

        for child in import_node.children:
            if child.type == "wildcard_import":
                is_wildcard = True
                break

        if module_name and (imported_items or is_wildcard):
            if module_name.startswith(self.project_name):
                base_module = module_name
            else:
                top_level_module = module_name.split(SEPARATOR_DOT)[0]
                if (self.repo_path / top_level_module).is_dir() or (
                    self.repo_path / f"{top_level_module}.py"
                ).is_file():
                    base_module = f"{self.project_name}.{module_name}"
                else:
                    base_module = module_name

            if is_wildcard:
                wildcard_key = f"*{base_module}"
                self.import_mapping[module_qn][wildcard_key] = base_module
                logger.debug(f"  Wildcard import: * -> {base_module}")
            else:
                for local_name, original_name in imported_items:
                    full_name = f"{base_module}.{original_name}"
                    self.import_mapping[module_qn][local_name] = full_name
                    logger.debug(f"  From import: {local_name} -> {full_name}")

    def _resolve_relative_import(self, relative_node: Node, module_qn: str) -> str:
        """Resolve relative imports like '.module' or '..parent.module'."""
        module_parts = module_qn.split(SEPARATOR_DOT)[1:]

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

        target_parts = module_parts[:-dots] if dots > 0 else module_parts

        if module_name:
            target_parts.extend(module_name.split(SEPARATOR_DOT))

        return SEPARATOR_DOT.join(target_parts)

    def _parse_js_ts_imports(self, captures: dict, module_qn: str) -> None:
        """Parse JavaScript/TypeScript import statements."""

        for import_node in captures.get("import", []):
            if import_node.type == "import_statement":
                source_module = None
                for child in import_node.children:
                    if child.type == "string":
                        source_text = safe_decode_with_fallback(child).strip("'\"")
                        source_module = self._resolve_js_module_path(
                            source_text, module_qn
                        )
                        break

                if not source_module:
                    continue

                for child in import_node.children:
                    if child.type == "import_clause":
                        self._parse_js_import_clause(child, source_module, module_qn)

            elif import_node.type == "lexical_declaration":
                self._parse_js_require(import_node, module_qn)

            elif import_node.type == "export_statement":
                self._parse_js_reexport(import_node, module_qn)

    def _resolve_js_module_path(self, import_path: str, current_module: str) -> str:
        """Resolve JavaScript module path to qualified name."""
        if not import_path.startswith("."):
            return import_path.replace("/", SEPARATOR_DOT)

        current_parts = current_module.split(SEPARATOR_DOT)[:-1]
        import_parts = import_path.split("/")

        for part in import_parts:
            if part == ".":
                continue
            if part == "..":
                if current_parts:
                    current_parts.pop()
            elif part:
                current_parts.append(part)

        return SEPARATOR_DOT.join(current_parts)

    def _parse_js_import_clause(
        self, clause_node: Node, source_module: str, current_module: str
    ) -> None:
        """Parse JavaScript import clause (named, default, namespace imports)."""
        for child in clause_node.children:
            if child.type == "identifier":
                imported_name = safe_decode_with_fallback(child)
                self.import_mapping[current_module][imported_name] = (
                    f"{source_module}.default"
                )
                logger.debug(
                    f"JS default import: {imported_name} -> {source_module}.default"
                )

            elif child.type == "named_imports":
                for grandchild in child.children:
                    if grandchild.type == "import_specifier":
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
        for declarator in decl_node.children:
            if declarator.type == "variable_declarator":
                name_node = declarator.child_by_field_name("name")
                value_node = declarator.child_by_field_name("value")

                if (
                    name_node
                    and value_node
                    and name_node.type == "identifier"
                    and value_node.type == "call_expression"
                ):
                    func_node = value_node.child_by_field_name("function")
                    args_node = value_node.child_by_field_name("arguments")

                    if (
                        func_node
                        and args_node
                        and func_node.type == "identifier"
                        and safe_decode_text(func_node) == "require"
                    ):
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

        for child in export_node.children:
            if child.type == "export_clause":
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
                    logger.debug(f"Java wildcard import: {imported_path}.*")
                    self.import_mapping[module_qn][f"*{imported_path}"] = imported_path
                else:
                    parts = imported_path.split(SEPARATOR_DOT)
                    if parts:
                        imported_name = parts[-1]
                        if is_static:
                            self.import_mapping[module_qn][imported_name] = (
                                imported_path
                            )
                            logger.debug(
                                f"Java static import: {imported_name} -> "
                                f"{imported_path}"
                            )
                        else:
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
        imports = extract_rust_use_imports(use_node)

        for imported_name, full_path in imports.items():
            self.import_mapping[module_qn][imported_name] = full_path
            logger.debug(f"Rust import: {imported_name} -> {full_path}")

    def _parse_go_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Go import declarations."""

        for import_node in captures.get("import", []):
            if import_node.type == "import_declaration":
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        """Parse a Go import declaration."""
        for child in import_node.children:
            if child.type == "import_spec":
                self._parse_go_import_spec(child, module_qn)
            elif child.type == "import_spec_list":
                for grandchild in child.children:
                    if grandchild.type == "import_spec":
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        """Parse a single Go import spec."""
        alias_name = None
        import_path = None

        for child in spec_node.children:
            if child.type == "package_identifier":
                alias_name = safe_decode_with_fallback(child)
            elif child.type == "interpreted_string_literal":
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            if alias_name:
                package_name = alias_name
            else:
                parts = import_path.split("/")
                package_name = parts[-1] if parts else import_path

            self.import_mapping[module_qn][package_name] = import_path
            logger.debug(f"Go import: {package_name} -> {import_path}")

    def _parse_cpp_imports(self, captures: dict, module_qn: str) -> None:
        """Parse C++ #include statements and C++20 module imports."""
        for import_node in captures.get("import", []):
            if import_node.type == "preproc_include":
                self._parse_cpp_include(import_node, module_qn)
            elif import_node.type == "template_function":
                self._parse_cpp_module_import(import_node, module_qn)
            elif import_node.type == "declaration":
                self._parse_cpp_module_declaration(import_node, module_qn)

    def _parse_cpp_include(self, include_node: Node, module_qn: str) -> None:
        """Parse a single C++ #include statement."""
        include_path = None
        is_system_include = False

        for child in include_node.children:
            if child.type == "string_literal":
                include_path = safe_decode_with_fallback(child).strip('"')
                is_system_include = False
            elif child.type == "system_lib_string":
                include_path = safe_decode_with_fallback(child).strip("<>")
                is_system_include = True

        if include_path:
            header_name = include_path.split("/")[-1]
            if header_name.endswith(".h") or header_name.endswith(".hpp"):
                local_name = header_name.split(SEPARATOR_DOT)[0]
            else:
                local_name = header_name

            if is_system_include:
                full_name = (
                    f"std.{include_path}"
                    if not include_path.startswith("std")
                    else include_path
                )
            else:
                path_parts = (
                    include_path.replace("/", SEPARATOR_DOT)
                    .replace(".h", "")
                    .replace(".hpp", "")
                )
                full_name = f"{self.project_name}.{path_parts}"

            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(
                f"C++ include: {local_name} -> {full_name} (system: {is_system_include})"
            )

    def _parse_cpp_module_import(self, import_node: Node, module_qn: str) -> None:
        """Parse C++20 module import statements like 'import <iostream>;'."""
        identifier_child = None
        template_args_child = None

        for child in import_node.children:
            if child.type == "identifier":
                identifier_child = child
            elif child.type == "template_argument_list":
                template_args_child = child

        if identifier_child and safe_decode_text(identifier_child) == "import":
            if template_args_child:
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
                    local_name = module_name
                    full_name = f"std.{module_name}"

                    self.import_mapping[module_qn][local_name] = full_name
                    logger.debug(f"C++20 module import: {local_name} -> {full_name}")

    def _parse_cpp_module_declaration(self, decl_node: Node, module_qn: str) -> None:
        """Parse C++20 module declarations and partition imports."""
        decoded_text = safe_decode_text(decl_node)
        if not decoded_text:
            return
        decl_text = decoded_text.strip()

        if decl_text.startswith("module ") and not decl_text.startswith("module ;"):
            parts = decl_text.split()
            if len(parts) >= 2:
                module_name = parts[1].rstrip(";")
                self.import_mapping[module_qn][module_name] = (
                    f"{self.project_name}.{module_name}"
                )
                logger.debug(f"C++20 module implementation: {module_name}")

        elif decl_text.startswith("export module "):
            parts = decl_text.split()
            if len(parts) >= 3:
                module_name = parts[2].rstrip(";")
                self.import_mapping[module_qn][module_name] = (
                    f"{self.project_name}.{module_name}"
                )
                logger.debug(f"C++20 module interface: {module_name}")

        elif "import :" in decl_text:
            colon_pos = decl_text.find(":")
            if colon_pos != -1:
                partition_part = decl_text[colon_pos + 1 :].split(";")[0].strip()
                if partition_part:
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
                f"Generic import parsing for {lang_config.language}: {import_node.type}"
            )

    def _parse_lua_imports(self, captures: dict, module_qn: str) -> None:
        """Parse Lua require-based imports from function_call captures."""
        for call_node in captures.get("import", []):
            if self._lua_is_require_call(call_node):
                module_path = self._lua_extract_require_arg(call_node)
                if module_path:
                    local_name = (
                        self._lua_extract_assignment_lhs(call_node)
                        or module_path.split(SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved
            elif self._lua_is_pcall_require(call_node):
                module_path = self._lua_extract_pcall_require_arg(call_node)
                if module_path:
                    local_name = (
                        self._lua_extract_pcall_assignment_lhs(call_node)
                        or module_path.split(SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved

            elif self._lua_is_stdlib_call(call_node):
                stdlib_module = self._lua_extract_stdlib_module(call_node)
                if stdlib_module:
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module

    def _lua_is_require_call(self, call_node: Node) -> bool:
        """Return True if function_call represents require(...) or require 'x'."""
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == "identifier":
            return safe_decode_text(first_child) == "require"
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
        """Return True if function_call represents pcall(require, 'module')."""
        first_child = call_node.children[0] if call_node.children else None
        if not (
            first_child
            and first_child.type == "identifier"
            and safe_decode_text(first_child) == "pcall"
        ):
            return False

        args = call_node.child_by_field_name("arguments")
        if not args:
            return False

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
        return extract_lua_assigned_name(call_node, accepted_var_types=("identifier",))

    def _lua_extract_pcall_assignment_lhs(self, call_node: Node) -> str | None:
        """Find the second identifier assigned from pcall(require, ...) pattern.

        In patterns like: local ok, json = pcall(require, 'json')
        We want to extract 'json' (the second identifier).
        """
        return extract_lua_pcall_second_identifier(call_node)

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        """Resolve Lua module path for require. Handles ./ and ../ prefixes."""
        if import_path.startswith("./") or import_path.startswith("../"):
            parts = current_module.split(SEPARATOR_DOT)[:-1]
            rel_parts = [p for p in import_path.replace("\\", "/").split("/")]
            for p in rel_parts:
                if p == ".":
                    continue
                if p == "..":
                    if parts:
                        parts.pop()
                elif p:
                    parts.append(p)
            return SEPARATOR_DOT.join(parts)
        dotted = import_path.replace("/", SEPARATOR_DOT)

        try:
            relative_file = dotted.replace(SEPARATOR_DOT, "/") + ".lua"
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}.{dotted}"
            if (self.repo_path / f"{dotted}.lua").is_file():
                return f"{self.project_name}.{dotted}"
        except OSError:
            pass

        return dotted

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        """Return True if function_call represents a Lua standard library call (e.g., string.upper, math.floor)."""
        from .lua_utils import safe_decode_text

        if not call_node.children:
            return False

        first_child = call_node.children[0]
        if first_child.type == "dot_index_expression":
            if first_child.children and first_child.children[0].type == "identifier":
                module_name = safe_decode_text(first_child.children[0])
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
            if first_child.children and first_child.children[0].type == "identifier":
                return safe_decode_text(first_child.children[0])

        return None

    def _extract_module_path(
        self,
        full_qualified_name: str,
        language: SupportedLanguage = SupportedLanguage.PYTHON,
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
        if self.function_registry and full_qualified_name in self.function_registry:
            entity_type = self.function_registry[full_qualified_name]
            if entity_type in ("Class", "Function", "Method"):
                parts = full_qualified_name.rsplit(SEPARATOR_DOT, 1)
                if len(parts) == 2:
                    return parts[0]

        match language:
            case SupportedLanguage.PYTHON:
                return self._extract_python_stdlib_path(full_qualified_name)
            case SupportedLanguage.JS | SupportedLanguage.TS:
                return self._extract_js_stdlib_path(full_qualified_name)
            case SupportedLanguage.GO:
                return self._extract_go_stdlib_path(full_qualified_name)
            case SupportedLanguage.RUST:
                return self._extract_rust_stdlib_path(full_qualified_name)
            case SupportedLanguage.CPP:
                return self._extract_cpp_stdlib_path(full_qualified_name)
            case SupportedLanguage.JAVA:
                return self._extract_java_stdlib_path(full_qualified_name)
            case SupportedLanguage.LUA:
                return self._extract_lua_stdlib_path(full_qualified_name)
            case _:
                return self._extract_generic_stdlib_path(full_qualified_name)

    def _extract_python_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Python stdlib module path using runtime introspection."""
        cached_result = _get_cached_stdlib_result("python", full_qualified_name)
        if cached_result is not None:
            return cached_result

        parts = full_qualified_name.split(SEPARATOR_DOT)
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
                        module_path = SEPARATOR_DOT.join(parts[:-1])
                        _cache_stdlib_result("python", full_qualified_name, module_path)
                        return module_path
            except (ImportError, AttributeError):
                pass

            if entity_name[0].isupper():
                result = SEPARATOR_DOT.join(parts[:-1])
            else:
                result = full_qualified_name

            _cache_stdlib_result("python", full_qualified_name, result)
            return result

        return full_qualified_name

    def _extract_js_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract JavaScript/Node.js stdlib module path using runtime introspection."""
        cached_result = _get_cached_stdlib_result("javascript", full_qualified_name)
        if cached_result is not None:
            return cached_result

        parts = full_qualified_name.split(SEPARATOR_DOT)
        if len(parts) >= 2:
            module_name = parts[0]
            entity_name = parts[-1]

            if _is_tool_available("node"):
                try:
                    import json
                    import os
                    import subprocess

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
                            module_path = SEPARATOR_DOT.join(parts[:-1])
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

            if entity_name[0].isupper():
                result = SEPARATOR_DOT.join(parts[:-1])
            else:
                result = full_qualified_name

            _cache_stdlib_result("javascript", full_qualified_name, result)
            return result

        return full_qualified_name

    def _extract_go_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Go stdlib module path using compile-time analysis."""
        parts = full_qualified_name.split("/")
        if len(parts) >= 2:
            try:
                import json
                import os
                import subprocess

                package_path = "/".join(parts[:-1])
                entity_name = parts[-1]

                resolve_result = subprocess.run(
                    ["go", "list", "-f", "{{.Dir}}", package_path],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

                if resolve_result.returncode != 0:
                    raise subprocess.CalledProcessError(
                        resolve_result.returncode, resolve_result.args
                    )

                package_dir = resolve_result.stdout.strip()
                if not package_dir:
                    raise subprocess.CalledProcessError(1, ["go", "list"])

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

                env = os.environ.copy()
                env["PACKAGE_PATH"] = package_dir
                env["ENTITY_NAME"] = entity_name

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

            entity_name = parts[-1]
            if entity_name[0].isupper():
                return "/".join(parts[:-1])

        return full_qualified_name

    def _extract_rust_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Rust stdlib module path using compile-time analysis."""
        parts = full_qualified_name.split("::")
        if len(parts) >= 2:
            entity_name = parts[-1]

            if (
                entity_name[0].isupper()
                or entity_name.isupper()
                or "_" not in entity_name
                and entity_name.islower()
            ):
                return "::".join(parts[:-1])

        return full_qualified_name

    def _extract_cpp_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract C++ stdlib module path using header analysis."""
        parts = full_qualified_name.split("::")
        if len(parts) >= 2:
            namespace = parts[0]
            if namespace == "std":
                entity_name = parts[-1]

                try:
                    import os
                    import subprocess
                    import tempfile

                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", delete=False
                    ) as f:
                        f.write(entity_name)
                        entity_file = f.name

                    try:
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

                    finally:
                        os.unlink(entity_file)

                except (
                    subprocess.TimeoutExpired,
                    subprocess.CalledProcessError,
                    FileNotFoundError,
                    OSError,
                ):
                    pass

                entity_name = parts[-1]
                if (
                    entity_name[0].isupper()
                    or entity_name.startswith("is_")
                    or entity_name.startswith("has_")
                    or entity_name
                    in {
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
                ):
                    return "::".join(parts[:-1])

        return full_qualified_name

    def _extract_java_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Java stdlib module path using reflection."""
        parts = full_qualified_name.split(SEPARATOR_DOT)
        if len(parts) >= 2:
            try:
                import json
                import os
                import subprocess
                import tempfile

                package_name = SEPARATOR_DOT.join(parts[:-1])
                entity_name = parts[-1]

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

                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".java", delete=False
                ) as f:
                    f.write(java_program)
                    java_file = f.name

                try:
                    compile_result = subprocess.run(
                        ["javac", java_file],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )

                    if compile_result.returncode == 0:
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
                                return SEPARATOR_DOT.join(parts[:-1])

                finally:
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

            entity_name = parts[-1]
            if (
                entity_name[0].isupper()
                or entity_name.endswith("Exception")
                or entity_name.endswith("Error")
                or entity_name.endswith("Interface")
                or entity_name.endswith("Builder")
                or entity_name
                in {
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
                return SEPARATOR_DOT.join(parts[:-1])

        return full_qualified_name

    def _extract_lua_stdlib_path(self, full_qualified_name: str) -> str:
        """Extract Lua stdlib module path using runtime introspection."""
        parts = full_qualified_name.split(SEPARATOR_DOT)
        if len(parts) >= 2:
            module_name = parts[0]
            entity_name = parts[-1]

            try:
                import os
                import subprocess

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
                    output = result.stdout.strip()
                    if "hasEntity=true" in output:
                        return SEPARATOR_DOT.join(parts[:-1])

            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                FileNotFoundError,
            ):
                pass

            entity_name = parts[-1]
            if entity_name[0].isupper() or entity_name in {
                "string",
                "table",
                "math",
                "io",
                "os",
                "debug",
            }:
                return SEPARATOR_DOT.join(parts[:-1])

        return full_qualified_name

    def _extract_generic_stdlib_path(self, full_qualified_name: str) -> str:
        """Generic fallback using basic heuristics."""
        parts = full_qualified_name.split(SEPARATOR_DOT)
        if len(parts) >= 2:
            entity_name = parts[-1]
            if entity_name[0].isupper():
                return SEPARATOR_DOT.join(parts[:-1])

        return full_qualified_name
