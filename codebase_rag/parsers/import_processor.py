from pathlib import Path

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, LanguageQueries
from .lua import utils as lua_utils
from .rs import utils as rs_utils
from .stdlib_extractor import (
    StdlibCacheStats,
    StdlibExtractor,
    clear_stdlib_cache,
    flush_stdlib_cache,
    get_stdlib_cache_stats,
    load_persistent_cache,
    save_persistent_cache,
)
from .utils import get_query_cursor, safe_decode_text, safe_decode_with_fallback


class ImportProcessor:
    def __init__(
        self,
        repo_path: Path,
        project_name: str,
        ingestor: IngestorProtocol | None = None,
        function_registry: FunctionRegistryTrieProtocol | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.project_name = project_name
        self.ingestor = ingestor
        self.function_registry = function_registry
        self.import_mapping: dict[str, dict[str, str]] = {}
        self.stdlib_extractor = StdlibExtractor(
            function_registry, repo_path, project_name
        )

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
    def get_stdlib_cache_stats() -> StdlibCacheStats:
        return get_stdlib_cache_stats()

    def parse_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in queries:
            return
        imports_query = queries[language]["imports"]
        if not imports_query:
            return

        lang_config = queries[language]["config"]

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
                ls.IMP_PARSED_COUNT.format(
                    count=len(self.import_mapping[module_qn]), module=module_qn
                )
            )

            if self.ingestor:
                for full_name in self.import_mapping[module_qn].values():
                    module_path = self._resolve_module_path(
                        full_name, module_qn, language
                    )

                    self.ingestor.ensure_relationship_batch(
                        (
                            cs.NodeLabel.MODULE,
                            cs.KEY_QUALIFIED_NAME,
                            module_qn,
                        ),
                        cs.RelationshipType.IMPORTS,
                        (
                            cs.NodeLabel.MODULE,
                            cs.KEY_QUALIFIED_NAME,
                            module_path,
                        ),
                    )
                    logger.debug(
                        ls.IMP_CREATED_RELATIONSHIP.format(
                            from_module=module_qn,
                            to_module=module_path,
                            full_name=full_name,
                        )
                    )

        except Exception as e:
            logger.warning(ls.IMP_PARSE_FAILED.format(module=module_qn, error=e))

    def _parse_python_imports(self, captures: dict, module_qn: str) -> None:
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(
            cs.CAPTURE_IMPORT_FROM, []
        )
        for import_node in all_imports:
            if import_node.type == cs.TS_PY_IMPORT_STATEMENT:
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
                self._handle_python_import_from_statement(import_node, module_qn)

    def _handle_python_import_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        for child in import_node.named_children:
            match child.type:
                case cs.TS_DOTTED_NAME:
                    self._handle_dotted_name_import(child, module_qn)
                case cs.TS_ALIASED_IMPORT:
                    self._handle_aliased_import(child, module_qn)

    def _handle_dotted_name_import(self, child: Node, module_qn: str) -> None:
        module_name = safe_decode_text(child) or ""
        local_name = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, local_name)
        self.import_mapping[module_qn][local_name] = full_name
        logger.debug(ls.IMP_IMPORT.format(local=local_name, full=full_name))

    def _handle_aliased_import(self, child: Node, module_qn: str) -> None:
        module_name_node = child.child_by_field_name(cs.FIELD_NAME)
        alias_node = child.child_by_field_name(cs.FIELD_ALIAS)
        if not module_name_node or not alias_node:
            return

        module_name = safe_decode_text(module_name_node)
        alias = safe_decode_text(alias_node)
        if not module_name or not alias:
            return

        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, top_level)
        self.import_mapping[module_qn][alias] = full_name
        logger.debug(ls.IMP_ALIASED_IMPORT.format(alias=alias, full=full_name))

    def _resolve_import_full_name(self, module_name: str, top_level: str) -> str:
        if self._is_local_module(top_level):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        return module_name

    def _is_local_module(self, module_name: str) -> bool:
        return (
            (self.repo_path / module_name).is_dir()
            or (self.repo_path / f"{module_name}{cs.EXT_PY}").is_file()
            or (self.repo_path / module_name / cs.INIT_PY).is_file()
        )

    def _is_local_java_import(self, import_path: str) -> bool:
        top_level = import_path.split(cs.SEPARATOR_DOT)[0]
        return (self.repo_path / top_level).is_dir()

    def _resolve_java_import_path(self, import_path: str) -> str:
        if self._is_local_java_import(import_path):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{import_path}"
        return import_path

    def _is_local_js_import(self, full_name: str) -> bool:
        return full_name.startswith(self.project_name + cs.SEPARATOR_DOT)

    def _resolve_js_internal_module(self, full_name: str) -> str:
        if full_name.endswith(cs.IMPORT_DEFAULT_SUFFIX):
            return full_name[: -len(cs.IMPORT_DEFAULT_SUFFIX)]

        parts = full_name.split(cs.SEPARATOR_DOT)
        if len(parts) <= 2:
            return full_name

        potential_module = cs.SEPARATOR_DOT.join(parts[:-1])
        relative_path = cs.SEPARATOR_SLASH.join(parts[1:-1])

        for ext in (cs.EXT_JS, cs.EXT_TS, cs.EXT_JSX, cs.EXT_TSX):
            if (self.repo_path / f"{relative_path}{ext}").is_file():
                return potential_module
            index_path = self.repo_path / relative_path / f"{cs.INDEX_INDEX}{ext}"
            if index_path.is_file():
                return potential_module

        return full_name

    def _is_local_rust_import(self, import_path: str) -> bool:
        return import_path.startswith(cs.RUST_CRATE_PREFIX)

    def _ensure_external_module_node(self, module_path: str, full_name: str) -> None:
        if not self.ingestor or not module_path:
            return
        if cs.SEPARATOR_DOUBLE_COLON in module_path:
            name = module_path.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
        else:
            name = module_path.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.MODULE,
            {
                cs.KEY_NAME: name,
                cs.KEY_QUALIFIED_NAME: module_path,
                cs.KEY_PATH: full_name,
                cs.KEY_IS_EXTERNAL: True,
                cs.KEY_PROJECT_NAME: cs.EXTERNAL_PROJECT_NAME,
            },
        )

    def _resolve_rust_import_path(self, import_path: str, module_qn: str) -> str:
        # (H) crate:: is always relative to the crate root, not the current module.
        # (H) We find the src directory in the qualified name to identify the crate root.
        if self._is_local_rust_import(import_path):
            path_without_crate = import_path[len(cs.RUST_CRATE_PREFIX) :]
            module_parts = module_qn.split(cs.SEPARATOR_DOT)
            try:
                src_index = module_parts.index(cs.LANG_SRC_DIR)
                crate_root_qn = cs.SEPARATOR_DOT.join(module_parts[: src_index + 1])
            except ValueError:
                crate_root_qn = self.project_name
            module_part = path_without_crate.split(cs.SEPARATOR_DOUBLE_COLON)[0]
            return f"{crate_root_qn}{cs.SEPARATOR_DOT}{module_part}"

        parts = import_path.split(cs.SEPARATOR_DOUBLE_COLON)
        module_path = (
            cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1]) if len(parts) > 1 else parts[0]
        )

        self._ensure_external_module_node(module_path, import_path)
        return module_path

    def _resolve_module_path(
        self,
        full_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> str:
        project_prefix = self.project_name + cs.SEPARATOR_DOT
        match language:
            # (H) Java MODULE semantics: Internal imports point to file-level MODULE
            # (H) nodes (e.g., project.utils.StringUtils) because Java files are named
            # (H) after their primary class. External imports point to package-level
            # (H) (e.g., java.util) because we lack source code to create file-level
            # (H) nodes. This asymmetry is intentional.
            case cs.SupportedLanguage.JAVA:
                if full_name.startswith(project_prefix):
                    return full_name
            case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS:
                if self._is_local_js_import(full_name):
                    return self._resolve_js_internal_module(full_name)
            case cs.SupportedLanguage.RUST:
                return self._resolve_rust_import_path(full_name, module_qn)

        module_path = self.stdlib_extractor.extract_module_path(full_name, language)
        if not module_path.startswith(project_prefix):
            self._ensure_external_module_node(module_path, full_name)
        return module_path

    def _handle_python_import_from_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        module_name = self._extract_python_from_module_name(import_node, module_qn)
        if not module_name:
            return

        imported_items = self._extract_python_imported_items(import_node)
        is_wildcard = any(
            child.type == cs.TS_WILDCARD_IMPORT for child in import_node.children
        )

        if not imported_items and not is_wildcard:
            return

        base_module = self._resolve_python_base_module(module_name)
        self._register_python_from_imports(
            module_qn, base_module, imported_items, is_wildcard
        )

    def _extract_python_from_module_name(
        self, import_node: Node, module_qn: str
    ) -> str | None:
        module_name_node = import_node.child_by_field_name(cs.FIELD_MODULE_NAME)
        if not module_name_node:
            return None

        if module_name_node.type == cs.TS_DOTTED_NAME:
            return safe_decode_text(module_name_node)
        if module_name_node.type == cs.TS_RELATIVE_IMPORT:
            return self._resolve_relative_import(module_name_node, module_qn)
        return None

    def _extract_python_imported_items(
        self, import_node: Node
    ) -> list[tuple[str, str]]:
        imported_items: list[tuple[str, str]] = []

        for name_node in import_node.children_by_field_name(cs.FIELD_NAME):
            if item := self._extract_single_python_import(name_node):
                imported_items.append(item)

        return imported_items

    def _extract_single_python_import(self, name_node: Node) -> tuple[str, str] | None:
        if name_node.type == cs.TS_DOTTED_NAME:
            if name := safe_decode_text(name_node):
                return (name, name)
        elif name_node.type == cs.TS_ALIASED_IMPORT:
            original_node = name_node.child_by_field_name(cs.FIELD_NAME)
            alias_node = name_node.child_by_field_name(cs.FIELD_ALIAS)
            if original_node and alias_node:
                original = safe_decode_text(original_node)
                alias = safe_decode_text(alias_node)
                if original and alias:
                    return (alias, original)
        return None

    def _resolve_python_base_module(self, module_name: str) -> str:
        if module_name.startswith(self.project_name):
            return module_name
        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        return self._resolve_import_full_name(module_name, top_level)

    def _register_python_from_imports(
        self,
        module_qn: str,
        base_module: str,
        imported_items: list[tuple[str, str]],
        is_wildcard: bool,
    ) -> None:
        if is_wildcard:
            wildcard_key = f"*{base_module}"
            self.import_mapping[module_qn][wildcard_key] = base_module
            logger.debug(ls.IMP_WILDCARD_IMPORT.format(module=base_module))
            return

        for local_name, original_name in imported_items:
            full_name = f"{base_module}{cs.SEPARATOR_DOT}{original_name}"
            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(ls.IMP_FROM_IMPORT.format(local=local_name, full=full_name))

    def _resolve_relative_import(self, relative_node: Node, module_qn: str) -> str:
        module_parts = module_qn.split(cs.SEPARATOR_DOT)[1:]

        dots = 0
        module_name = ""

        for child in relative_node.children:
            if child.type == cs.TS_IMPORT_PREFIX:
                if decoded_text := safe_decode_text(child):
                    dots = len(decoded_text)
            elif child.type == cs.TS_DOTTED_NAME:
                if decoded_name := safe_decode_text(child):
                    module_name = decoded_name

        target_parts = module_parts[:-dots] if dots > 0 else module_parts

        if module_name:
            target_parts.extend(module_name.split(cs.SEPARATOR_DOT))

        return cs.SEPARATOR_DOT.join(target_parts)

    def _parse_js_ts_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_IMPORT_STATEMENT:
                source_module = None
                for child in import_node.children:
                    if child.type == cs.TS_STRING:
                        source_text = safe_decode_with_fallback(child).strip("'\"")
                        source_module = self._resolve_js_module_path(
                            source_text, module_qn
                        )
                        break

                if not source_module:
                    continue

                for child in import_node.children:
                    if child.type == cs.TS_IMPORT_CLAUSE:
                        self._parse_js_import_clause(child, source_module, module_qn)

            elif import_node.type == cs.TS_LEXICAL_DECLARATION:
                self._parse_js_require(import_node, module_qn)

            elif import_node.type == cs.TS_EXPORT_STATEMENT:
                self._parse_js_reexport(import_node, module_qn)

    def _resolve_js_module_path(self, import_path: str, current_module: str) -> str:
        if not import_path.startswith(cs.PATH_CURRENT_DIR):
            return import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        current_parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
        import_parts = import_path.split(cs.SEPARATOR_SLASH)

        for part in import_parts:
            if part == cs.PATH_CURRENT_DIR:
                continue
            if part == cs.PATH_PARENT_DIR:
                if current_parts:
                    current_parts.pop()
            elif part:
                current_parts.append(part)

        return cs.SEPARATOR_DOT.join(current_parts)

    def _parse_js_import_clause(
        self, clause_node: Node, source_module: str, current_module: str
    ) -> None:
        for child in clause_node.children:
            if child.type == cs.TS_IDENTIFIER:
                imported_name = safe_decode_with_fallback(child)
                self.import_mapping[current_module][imported_name] = (
                    f"{source_module}{cs.IMPORT_DEFAULT_SUFFIX}"
                )
                logger.debug(
                    ls.IMP_JS_DEFAULT.format(name=imported_name, module=source_module)
                )

            elif child.type == cs.TS_NAMED_IMPORTS:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPECIFIER:
                        name_node = grandchild.child_by_field_name(cs.FIELD_NAME)
                        alias_node = grandchild.child_by_field_name(cs.FIELD_ALIAS)
                        if name_node:
                            imported_name = safe_decode_with_fallback(name_node)
                            local_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
                                else imported_name
                            )
                            self.import_mapping[current_module][local_name] = (
                                f"{source_module}{cs.SEPARATOR_DOT}{imported_name}"
                            )
                            logger.debug(
                                ls.IMP_JS_NAMED.format(
                                    local=local_name,
                                    module=source_module,
                                    name=imported_name,
                                )
                            )

            elif child.type == cs.TS_NAMESPACE_IMPORT:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IDENTIFIER:
                        namespace_name = safe_decode_with_fallback(grandchild)
                        self.import_mapping[current_module][namespace_name] = (
                            source_module
                        )
                        logger.debug(
                            ls.IMP_JS_NAMESPACE.format(
                                name=namespace_name, module=source_module
                            )
                        )
                        break

    def _parse_js_require(self, decl_node: Node, current_module: str) -> None:
        for declarator in decl_node.children:
            if declarator.type == cs.TS_VARIABLE_DECLARATOR:
                name_node = declarator.child_by_field_name(cs.FIELD_NAME)
                value_node = declarator.child_by_field_name(cs.FIELD_VALUE)

                if (
                    name_node
                    and value_node
                    and name_node.type == cs.TS_IDENTIFIER
                    and value_node.type == cs.TS_CALL_EXPRESSION
                ):
                    func_node = value_node.child_by_field_name(cs.FIELD_FUNCTION)
                    args_node = value_node.child_by_field_name(cs.FIELD_ARGUMENTS)

                    if (
                        func_node
                        and args_node
                        and func_node.type == cs.TS_IDENTIFIER
                        and safe_decode_text(func_node) == cs.IMPORT_REQUIRE
                    ):
                        for arg in args_node.children:
                            if arg.type == cs.TS_STRING:
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
                                    ls.IMP_JS_REQUIRE.format(
                                        var=var_name, module=resolved_module
                                    )
                                )
                                break

    def _parse_js_reexport(self, export_node: Node, current_module: str) -> None:
        source_module = None
        for child in export_node.children:
            if child.type == cs.TS_STRING:
                source_text = safe_decode_with_fallback(child).strip("'\"")
                source_module = self._resolve_js_module_path(
                    source_text, current_module
                )
                break

        if not source_module:
            return

        for child in export_node.children:
            if child.type == cs.TS_ASTERISK:
                wildcard_key = f"*{source_module}"
                self.import_mapping[current_module][wildcard_key] = source_module
                logger.debug(ls.IMP_JS_NAMESPACE_REEXPORT.format(module=source_module))
            elif child.type == cs.TS_EXPORT_CLAUSE:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_EXPORT_SPECIFIER:
                        name_node = grandchild.child_by_field_name(cs.FIELD_NAME)
                        alias_node = grandchild.child_by_field_name(cs.FIELD_ALIAS)
                        if name_node:
                            original_name = safe_decode_with_fallback(name_node)
                            exported_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
                                else original_name
                            )
                            self.import_mapping[current_module][exported_name] = (
                                f"{source_module}{cs.SEPARATOR_DOT}{original_name}"
                            )
                            logger.debug(
                                ls.IMP_JS_REEXPORT.format(
                                    exported=exported_name,
                                    module=source_module,
                                    original=original_name,
                                )
                            )

    def _parse_java_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_IMPORT_DECLARATION:
                is_static = False
                imported_path = None
                is_wildcard = False

                for child in import_node.children:
                    if child.type == cs.TS_STATIC:
                        is_static = True
                    elif child.type == cs.TS_SCOPED_IDENTIFIER:
                        imported_path = safe_decode_with_fallback(child)
                    elif child.type == cs.TS_ASTERISK:
                        is_wildcard = True

                if not imported_path:
                    continue

                resolved_path = self._resolve_java_import_path(imported_path)

                if is_wildcard:
                    logger.debug(ls.IMP_JAVA_WILDCARD.format(path=resolved_path))
                    self.import_mapping[module_qn][f"*{resolved_path}"] = resolved_path
                elif parts := resolved_path.split(cs.SEPARATOR_DOT):
                    imported_name = parts[-1]
                    self.import_mapping[module_qn][imported_name] = resolved_path
                    if is_static:
                        logger.debug(
                            ls.IMP_JAVA_STATIC.format(
                                name=imported_name, path=resolved_path
                            )
                        )
                    else:
                        logger.debug(
                            ls.IMP_JAVA_IMPORT.format(
                                name=imported_name, path=resolved_path
                            )
                        )

    def _parse_rust_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_USE_DECLARATION:
                self._parse_rust_use_declaration(import_node, module_qn)

    def _parse_rust_use_declaration(self, use_node: Node, module_qn: str) -> None:
        imports = rs_utils.extract_use_imports(use_node)

        for imported_name, full_path in imports.items():
            self.import_mapping[module_qn][imported_name] = full_path
            logger.debug(ls.IMP_RUST.format(name=imported_name, path=full_path))

    def _parse_go_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_GO_IMPORT_DECLARATION:
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        for child in import_node.children:
            if child.type == cs.TS_IMPORT_SPEC:
                self._parse_go_import_spec(child, module_qn)
            elif child.type == cs.TS_IMPORT_SPEC_LIST:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPEC:
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        alias_name = None
        import_path = None

        for child in spec_node.children:
            if child.type == cs.TS_PACKAGE_IDENTIFIER:
                alias_name = safe_decode_with_fallback(child)
            elif child.type == cs.TS_INTERPRETED_STRING_LITERAL:
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            package_name = alias_name or import_path.split(cs.SEPARATOR_SLASH)[-1]
            self.import_mapping[module_qn][package_name] = import_path
            logger.debug(ls.IMP_GO.format(package=package_name, path=import_path))

    def _parse_cpp_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_PREPROC_INCLUDE:
                self._parse_cpp_include(import_node, module_qn)
            elif import_node.type == cs.TS_TEMPLATE_FUNCTION:
                self._parse_cpp_module_import(import_node, module_qn)
            elif import_node.type == cs.TS_DECLARATION:
                self._parse_cpp_module_declaration(import_node, module_qn)

    def _parse_cpp_include(self, include_node: Node, module_qn: str) -> None:
        include_path = None
        is_system_include = False

        for child in include_node.children:
            if child.type == cs.TS_STRING_LITERAL:
                include_path = safe_decode_with_fallback(child).strip('"')
                is_system_include = False
            elif child.type == cs.TS_SYSTEM_LIB_STRING:
                include_path = safe_decode_with_fallback(child).strip("<>")
                is_system_include = True

        if include_path:
            header_name = include_path.split(cs.SEPARATOR_SLASH)[-1]
            if header_name.endswith(cs.EXT_H) or header_name.endswith(cs.EXT_HPP):
                local_name = header_name.split(cs.SEPARATOR_DOT)[0]
            else:
                local_name = header_name

            if is_system_include:
                full_name = (
                    include_path
                    if include_path.startswith(cs.CPP_STD_PREFIX)
                    else f"{cs.IMPORT_STD_PREFIX}{include_path}"
                )
            else:
                path_parts = (
                    include_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
                    .replace(cs.EXT_H, "")
                    .replace(cs.EXT_HPP, "")
                )
                full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{path_parts}"

            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(
                ls.IMP_CPP_INCLUDE.format(
                    local=local_name, full=full_name, system=is_system_include
                )
            )

    def _parse_cpp_module_import(self, import_node: Node, module_qn: str) -> None:
        identifier_child = None
        template_args_child = None

        for child in import_node.children:
            if child.type == cs.TS_IDENTIFIER:
                identifier_child = child
            elif child.type == cs.TS_TEMPLATE_ARGUMENT_LIST:
                template_args_child = child

        if (
            identifier_child
            and safe_decode_text(identifier_child) == cs.IMPORT_IMPORT
            and template_args_child
        ):
            module_name = None
            for child in template_args_child.children:
                if child.type == cs.TS_TYPE_DESCRIPTOR:
                    for desc_child in child.children:
                        if desc_child.type == cs.TS_TYPE_IDENTIFIER:
                            module_name = safe_decode_with_fallback(desc_child)
                            break
                elif child.type == cs.TS_TYPE_IDENTIFIER:
                    module_name = safe_decode_with_fallback(child)

            if module_name:
                local_name = module_name
                full_name = f"{cs.IMPORT_STD_PREFIX}{module_name}"

                self.import_mapping[module_qn][local_name] = full_name
                logger.debug(ls.IMP_CPP_MODULE.format(local=local_name, full=full_name))

    def _parse_cpp_module_declaration(self, decl_node: Node, module_qn: str) -> None:
        decoded_text = safe_decode_text(decl_node)
        if not decoded_text:
            return
        decl_text = decoded_text.strip()

        if decl_text.startswith(cs.CPP_MODULE_PREFIX) and not decl_text.startswith(
            cs.CPP_MODULE_PRIVATE_PREFIX
        ):
            parts = decl_text.split()
            if len(parts) >= 2:
                self._register_cpp_module_mapping(
                    parts, 1, module_qn, ls.IMP_CPP_MODULE_IMPL
                )
        elif decl_text.startswith(cs.CPP_EXPORT_MODULE_PREFIX):
            parts = decl_text.split()
            if len(parts) >= 3:
                self._register_cpp_module_mapping(
                    parts, 2, module_qn, ls.IMP_CPP_MODULE_IFACE
                )
        elif cs.CPP_IMPORT_PARTITION_PREFIX in decl_text:
            colon_pos = decl_text.find(cs.SEPARATOR_COLON)
            if colon_pos != -1:
                if partition_part := decl_text[colon_pos + 1 :].split(";")[0].strip():
                    partition_name = f"{cs.CPP_PARTITION_PREFIX}{partition_part}"
                    full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{partition_part}"
                    self.import_mapping[module_qn][partition_name] = full_name
                    logger.debug(
                        ls.IMP_CPP_PARTITION.format(
                            partition=partition_name, full=full_name
                        )
                    )

    def _register_cpp_module_mapping(
        self, parts: list[str], name_index: int, module_qn: str, log_template: str
    ) -> None:
        module_name = parts[name_index].rstrip(";")
        self.import_mapping[module_qn][module_name] = (
            f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        )
        logger.debug(log_template.format(name=module_name))

    def _parse_generic_imports(
        self, captures: dict, module_qn: str, lang_config: LanguageSpec
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            logger.debug(
                ls.IMP_GENERIC.format(
                    language=lang_config.language, node_type=import_node.type
                )
            )

    def _parse_lua_imports(self, captures: dict, module_qn: str) -> None:
        for call_node in captures.get(cs.CAPTURE_IMPORT, []):
            if self._lua_is_require_call(call_node):
                if module_path := self._lua_extract_require_arg(call_node):
                    local_name = (
                        self._lua_extract_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved
            elif self._lua_is_pcall_require(call_node):
                if module_path := self._lua_extract_pcall_require_arg(call_node):
                    local_name = (
                        self._lua_extract_pcall_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved

            elif self._lua_is_stdlib_call(call_node):
                if stdlib_module := self._lua_extract_stdlib_module(call_node):
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module

    def _lua_is_require_call(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == cs.TS_IDENTIFIER:
            return safe_decode_text(first_child) == cs.IMPORT_REQUIRE
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if not (
            first_child
            and first_child.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_child) == cs.IMPORT_PCALL
        ):
            return False

        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return False

        first_arg_node = next(
            (
                child
                for child in args.children
                if child.type not in cs.PUNCTUATION_TYPES
            ),
            None,
        )

        return (
            first_arg_node is not None
            and first_arg_node.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_arg_node) == cs.IMPORT_REQUIRE
        )

    def _lua_extract_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        candidates = args.children if args else call_node.children
        for node in candidates:
            if node.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(node):
                    return decoded.strip("'\"")
        return None

    def _lua_extract_pcall_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return None
        found_require = False
        for child in args.children:
            if found_require and child.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(child):
                    return decoded.strip("'\"")
            if (
                child.type == cs.TS_IDENTIFIER
                and safe_decode_text(child) == cs.IMPORT_REQUIRE
            ):
                found_require = True
        return None

    def _lua_extract_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(
            call_node, accepted_var_types=(cs.TS_IDENTIFIER,)
        )

    def _lua_extract_pcall_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_pcall_second_identifier(call_node)

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        if import_path.startswith(cs.PATH_RELATIVE_PREFIX) or import_path.startswith(
            cs.PATH_PARENT_PREFIX
        ):
            parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
            rel_parts = list(
                import_path.replace("\\", cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH)
            )
            for p in rel_parts:
                if p == cs.PATH_CURRENT_DIR:
                    continue
                if p == cs.PATH_PARENT_DIR:
                    if parts:
                        parts.pop()
                elif p:
                    parts.append(p)
            return cs.SEPARATOR_DOT.join(parts)
        dotted = import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        try:
            relative_file = (
                dotted.replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH) + cs.EXT_LUA
            )
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
            if (self.repo_path / f"{dotted}{cs.EXT_LUA}").is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
        except OSError:
            pass

        return dotted

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        if not call_node.children:
            return False

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            module_name = safe_decode_text(first_child.children[0])
            return module_name in cs.LUA_STDLIB_MODULES

        return False

    def _lua_extract_stdlib_module(self, call_node: Node) -> str | None:
        if not call_node.children:
            return None

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            return safe_decode_text(first_child.children[0])

        return None
