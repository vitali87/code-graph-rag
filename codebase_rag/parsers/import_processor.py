from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from ..language_spec import LanguageSpec
from ..types_defs import LanguageQueries
from .lua_utils import (
    extract_lua_assigned_name,
    extract_lua_pcall_second_identifier,
)
from .rust_utils import extract_rust_use_imports
from .stdlib_extractor import (
    StdlibExtractor,
    clear_stdlib_cache,
    flush_stdlib_cache,
    get_stdlib_cache_stats,
    load_persistent_cache,
    save_persistent_cache,
)
from .utils import get_query_cursor, safe_decode_text, safe_decode_with_fallback

_JS_TYPESCRIPT_LANGUAGES = {cs.SupportedLanguage.JS, cs.SupportedLanguage.TS}


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
        self.stdlib_extractor = StdlibExtractor(function_registry)

        load_persistent_cache()

    def __del__(self) -> None:
        try:
            save_persistent_cache()
        except Exception:
            pass

    @staticmethod
    def flush_stdlib_cache() -> None:
        flush_stdlib_cache()

    @staticmethod
    def clear_stdlib_cache() -> None:
        clear_stdlib_cache()

    @staticmethod
    def get_stdlib_cache_stats() -> dict[str, Any]:
        return get_stdlib_cache_stats()

    def parse_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in queries or not queries[language].get("imports"):
            return

        lang_config = queries[language]["config"]
        imports_query = queries[language]["imports"]

        self.import_mapping[module_qn] = {}

        try:
            cursor = get_query_cursor(imports_query)
            captures = cursor.captures(root_node)

            match language:
                case cs.SupportedLanguage.PYTHON:
                    self._parse_python_imports(captures, module_qn)
                case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS:
                    self._parse_js_ts_imports(captures, module_qn)
                case cs.SupportedLanguage.JAVA:
                    self._parse_java_imports(captures, module_qn)
                case cs.SupportedLanguage.RUST:
                    self._parse_rust_imports(captures, module_qn)
                case cs.SupportedLanguage.GO:
                    self._parse_go_imports(captures, module_qn)
                case cs.SupportedLanguage.CPP:
                    self._parse_cpp_imports(captures, module_qn)
                case cs.SupportedLanguage.LUA:
                    self._parse_lua_imports(captures, module_qn)
                case _:
                    self._parse_generic_imports(captures, module_qn, lang_config)

            logger.debug(
                f"Parsed {len(self.import_mapping[module_qn])} imports in {module_qn}"
            )

            if self.ingestor:
                for local_name, full_name in self.import_mapping[module_qn].items():
                    module_path = self.stdlib_extractor.extract_module_path(
                        full_name, language
                    )

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
        for import_node in captures.get("import", []) + captures.get("import_from", []):
            if import_node.type == "import_statement":
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == "import_from_statement":
                self._handle_python_import_from_statement(import_node, module_qn)

    def _handle_python_import_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        for child in import_node.named_children:
            if child.type == "dotted_name":
                module_name = safe_decode_text(child) or ""
                local_name = module_name.split(cs.SEPARATOR_DOT)[0]

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

                    top_level_module = module_name.split(cs.SEPARATOR_DOT)[0]
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
                top_level_module = module_name.split(cs.SEPARATOR_DOT)[0]
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
        module_parts = module_qn.split(cs.SEPARATOR_DOT)[1:]

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
            target_parts.extend(module_name.split(cs.SEPARATOR_DOT))

        return cs.SEPARATOR_DOT.join(target_parts)

    def _parse_js_ts_imports(self, captures: dict, module_qn: str) -> None:
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
        if not import_path.startswith("."):
            return import_path.replace("/", cs.SEPARATOR_DOT)

        current_parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
        import_parts = import_path.split("/")

        for part in import_parts:
            if part == ".":
                continue
            if part == "..":
                if current_parts:
                    current_parts.pop()
            elif part:
                current_parts.append(part)

        return cs.SEPARATOR_DOT.join(current_parts)

    def _parse_js_import_clause(
        self, clause_node: Node, source_module: str, current_module: str
    ) -> None:
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
                    parts = imported_path.split(cs.SEPARATOR_DOT)
                    if parts:
                        imported_name = parts[-1]
                        self.import_mapping[module_qn][imported_name] = imported_path
                        import_type = "static import" if is_static else "import"
                        logger.debug(
                            f"Java {import_type}: {imported_name} -> {imported_path}"
                        )

    def _parse_rust_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get("import", []):
            if import_node.type == "use_declaration":
                self._parse_rust_use_declaration(import_node, module_qn)

    def _parse_rust_use_declaration(self, use_node: Node, module_qn: str) -> None:
        imports = extract_rust_use_imports(use_node)

        for imported_name, full_path in imports.items():
            self.import_mapping[module_qn][imported_name] = full_path
            logger.debug(f"Rust import: {imported_name} -> {full_path}")

    def _parse_go_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get("import", []):
            if import_node.type == "import_declaration":
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        for child in import_node.children:
            if child.type == "import_spec":
                self._parse_go_import_spec(child, module_qn)
            elif child.type == "import_spec_list":
                for grandchild in child.children:
                    if grandchild.type == "import_spec":
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        alias_name = None
        import_path = None

        for child in spec_node.children:
            if child.type == "package_identifier":
                alias_name = safe_decode_with_fallback(child)
            elif child.type == "interpreted_string_literal":
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            package_name = alias_name or import_path.split("/")[-1]
            self.import_mapping[module_qn][package_name] = import_path
            logger.debug(f"Go import: {package_name} -> {import_path}")

    def _parse_cpp_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get("import", []):
            if import_node.type == "preproc_include":
                self._parse_cpp_include(import_node, module_qn)
            elif import_node.type == "template_function":
                self._parse_cpp_module_import(import_node, module_qn)
            elif import_node.type == "declaration":
                self._parse_cpp_module_declaration(import_node, module_qn)

    def _parse_cpp_include(self, include_node: Node, module_qn: str) -> None:
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
                local_name = header_name.split(cs.SEPARATOR_DOT)[0]
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
                    include_path.replace("/", cs.SEPARATOR_DOT)
                    .replace(".h", "")
                    .replace(".hpp", "")
                )
                full_name = f"{self.project_name}.{path_parts}"

            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(
                f"C++ include: {local_name} -> {full_name} (system: {is_system_include})"
            )

    def _parse_cpp_module_import(self, import_node: Node, module_qn: str) -> None:
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
        self, captures: dict, module_qn: str, lang_config: LanguageSpec
    ) -> None:
        for import_node in captures.get("import", []):
            logger.debug(
                f"Generic import parsing for {lang_config.language}: {import_node.type}"
            )

    def _parse_lua_imports(self, captures: dict, module_qn: str) -> None:
        for call_node in captures.get("import", []):
            if self._lua_is_require_call(call_node):
                module_path = self._lua_extract_require_arg(call_node)
                if module_path:
                    local_name = (
                        self._lua_extract_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved
            elif self._lua_is_pcall_require(call_node):
                module_path = self._lua_extract_pcall_require_arg(call_node)
                if module_path:
                    local_name = (
                        self._lua_extract_pcall_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved

            elif self._lua_is_stdlib_call(call_node):
                stdlib_module = self._lua_extract_stdlib_module(call_node)
                if stdlib_module:
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module

    def _lua_is_require_call(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == "identifier":
            return safe_decode_text(first_child) == "require"
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
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
        args = call_node.child_by_field_name("arguments")
        candidates = args.children if args else call_node.children
        for node in candidates:
            if node.type in ("string", "string_literal"):
                decoded = safe_decode_text(node)
                if decoded:
                    return decoded.strip("'\"")
        return None

    def _lua_extract_pcall_require_arg(self, call_node: Node) -> str | None:
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
        return extract_lua_assigned_name(call_node, accepted_var_types=("identifier",))

    def _lua_extract_pcall_assignment_lhs(self, call_node: Node) -> str | None:
        return extract_lua_pcall_second_identifier(call_node)

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        if import_path.startswith("./") or import_path.startswith("../"):
            parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
            rel_parts = [p for p in import_path.replace("\\", "/").split("/")]
            for p in rel_parts:
                if p == ".":
                    continue
                if p == "..":
                    if parts:
                        parts.pop()
                elif p:
                    parts.append(p)
            return cs.SEPARATOR_DOT.join(parts)
        dotted = import_path.replace("/", cs.SEPARATOR_DOT)

        try:
            relative_file = dotted.replace(cs.SEPARATOR_DOT, "/") + ".lua"
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}.{dotted}"
            if (self.repo_path / f"{dotted}.lua").is_file():
                return f"{self.project_name}.{dotted}"
        except OSError:
            pass

        return dotted

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        if not call_node.children:
            return False

        first_child = call_node.children[0]
        if first_child.type == "dot_index_expression":
            if first_child.children and first_child.children[0].type == "identifier":
                module_name = safe_decode_text(first_child.children[0])
                return module_name in cs.LUA_STDLIB_MODULES

        return False

    def _lua_extract_stdlib_module(self, call_node: Node) -> str | None:
        if not call_node.children:
            return None

        first_child = call_node.children[0]
        if first_child.type == "dot_index_expression":
            if first_child.children and first_child.children[0].type == "identifier":
                return safe_decode_text(first_child.children[0])

        return None
