from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ... import logs
from ...types_defs import ASTNode, PropertyDict
from ...utils.path_utils import calculate_paths
from ..java import utils as java_utils
from ..py import resolve_class_name
from ..rs import utils as rs_utils
from ..utils import ingest_method, safe_decode_text
from . import cpp_modules
from . import identity as id_
from . import method_override as mo
from . import node_type as nt
from . import relationships as rel

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...services import IngestorProtocol
    from ...types_defs import (
        FunctionRegistryTrieProtocol,
        LanguageQueries,
        SimpleNameLookup,
    )
    from ..import_processor import ImportProcessor


class ClassIngestMixin:
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    class_inheritance: dict[str, list[str]]

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    @abstractmethod
    def _extract_decorators(self, node: ASTNode) -> list[str]: ...

    def _resolve_to_qn(self, name: str, module_qn: str) -> str:
        return self._resolve_class_name(name, module_qn) or f"{module_qn}.{name}"

    def _ingest_cpp_module_declarations(
        self,
        root_node: Node,
        module_qn: str,
        file_path: Path,
    ) -> None:
        cpp_modules.ingest_cpp_module_declarations(
            root_node,
            module_qn,
            file_path,
            self.repo_path,
            self.project_name,
            self.ingestor,
        )

    def _find_cpp_exported_classes(self, root_node: Node) -> list[Node]:
        return cpp_modules.find_cpp_exported_classes(root_node)

    def _ingest_classes_and_methods(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]
        if not (query := lang_queries[cs.QUERY_CLASSES]):
            return

        lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        class_nodes = captures.get(cs.CAPTURE_CLASS, [])
        module_nodes = captures.get(cs.ONEOF_MODULE, [])

        if language == cs.SupportedLanguage.CPP:
            class_nodes.extend(self._find_cpp_exported_classes(root_node))

        file_path = self.module_qn_to_file_path.get(module_qn)

        for class_node in class_nodes:
            if isinstance(class_node, Node):
                self._process_class_node(
                    class_node,
                    module_qn,
                    language,
                    lang_queries,
                    lang_config,
                    file_path,
                )

        self._process_inline_modules(module_nodes, module_qn, lang_config)

    def _process_class_node(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> None:
        if language == cs.SupportedLanguage.RUST and class_node.type == cs.TS_IMPL_ITEM:
            self._ingest_rust_impl_methods(
                class_node, module_qn, language, lang_queries
            )
            return

        identity = id_.resolve_class_identity(
            class_node,
            module_qn,
            language,
            lang_config,
            file_path,
            self.repo_path,
            self.project_name,
        )
        if not identity:
            return

        class_qn, class_name, is_exported = identity
        node_type = nt.determine_node_type(class_node, class_name, class_qn, language)

        class_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: class_qn,
            cs.KEY_NAME: class_name,
            cs.KEY_DECORATORS: self._extract_decorators(class_node),
            cs.KEY_START_LINE: class_node.start_point[0] + 1,
            cs.KEY_END_LINE: class_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: self._get_docstring(class_node),
            cs.KEY_IS_EXPORTED: is_exported,
        }

        if file_path:
            paths = calculate_paths(
                file_path=file_path,
                repo_path=self.repo_path,
            )
            class_props[cs.KEY_PATH] = paths["relative_path"]
            class_props[cs.KEY_ABSOLUTE_PATH] = paths["absolute_path"]
            class_props[cs.KEY_PROJECT_NAME] = self.project_name

        self.ingestor.ensure_node_batch(node_type, class_props)
        self.function_registry[class_qn] = node_type
        if class_name:
            self.simple_name_lookup[class_name].add(class_qn)

        rel.create_class_relationships(
            class_node,
            class_qn,
            module_qn,
            node_type,
            is_exported,
            language,
            self.class_inheritance,
            self.ingestor,
            self.import_processor,
            self._resolve_to_qn,
            self.function_registry,
        )
        self._ingest_class_methods(
            class_node, class_qn, language, lang_queries, file_path
        )

    def _ingest_rust_impl_methods(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
    ) -> None:
        if not (impl_target := rs_utils.extract_impl_target(class_node)):
            return

        class_qn = f"{module_qn}.{impl_target}"
        body_node = class_node.child_by_field_name("body")
        method_query = lang_queries[cs.QUERY_FUNCTIONS]

        if not body_node or not method_query:
            return

        method_cursor = QueryCursor(method_query)
        method_captures = method_cursor.captures(body_node)
        for method_node in method_captures.get(cs.CAPTURE_FUNCTION, []):
            if isinstance(method_node, Node):
                file_path = self.module_qn_to_file_path.get(module_qn)
                ingest_method(
                    method_node,
                    class_qn,
                    cs.NodeLabel.CLASS,
                    self.ingestor,
                    self.function_registry,
                    self.simple_name_lookup,
                    self._get_docstring,
                    language,
                    file_path=file_path,
                    repo_path=self.repo_path if file_path else None,
                    project_name=self.project_name,
                )

    def _ingest_class_methods(
        self,
        class_node: Node,
        class_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> None:
        body_node = class_node.child_by_field_name("body")
        method_query = lang_queries[cs.QUERY_FUNCTIONS]
        if not body_node or not method_query:
            return

        method_cursor = QueryCursor(method_query)
        method_captures = method_cursor.captures(body_node)
        for method_node in method_captures.get(cs.CAPTURE_FUNCTION, []):
            if not isinstance(method_node, Node):
                continue

            method_qualified_name = None
            if language == cs.SupportedLanguage.JAVA:
                method_info = java_utils.extract_method_info(method_node)
                if method_name := method_info.get(cs.KEY_NAME):
                    parameters = method_info.get(cs.KEY_PARAMETERS, [])
                    param_sig = (
                        f"({','.join(parameters)})" if parameters else cs.EMPTY_PARENS
                    )
                    method_qualified_name = f"{class_qn}.{method_name}{param_sig}"

            ingest_method(
                method_node,
                class_qn,
                cs.NodeLabel.CLASS,
                self.ingestor,
                self.function_registry,
                self.simple_name_lookup,
                self._get_docstring,
                language,
                self._extract_decorators,
                method_qualified_name,
                file_path=file_path,
                repo_path=self.repo_path if file_path else None,
                project_name=self.project_name,
            )

    def _process_inline_modules(
        self,
        module_nodes: list[Node],
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> None:
        for module_node in module_nodes:
            if not isinstance(module_node, Node):
                continue
            if not (module_name_node := module_node.child_by_field_name("name")):
                continue
            if not module_name_node.text:
                continue

            module_name = safe_decode_text(module_name_node)
            nested_qn = id_.build_nested_qualified_name_for_class(
                module_node, module_qn, module_name or "", lang_config
            )
            inline_module_qn = nested_qn or f"{module_qn}.{module_name}"

            module_props: PropertyDict = {
                cs.KEY_QUALIFIED_NAME: inline_module_qn,
                cs.KEY_NAME: module_name,
                cs.KEY_PATH: f"{cs.INLINE_MODULE_PATH_PREFIX}{module_name}",
            }
            logger.info(
                logs.CLASS_FOUND_INLINE_MODULE.format(
                    name=module_name, qn=inline_module_qn
                )
            )
            self.ingestor.ensure_node_batch(cs.NodeLabel.MODULE, module_props)

    def process_all_method_overrides(self) -> None:
        mo.process_all_method_overrides(
            self.function_registry,
            self.class_inheritance,
            self.ingestor,
        )

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _extract_cpp_base_class_name(self, parent_text: str) -> str:
        from . import parent_extraction as pe

        return pe.extract_cpp_base_class_name(parent_text)

    def _get_node_type_for_inheritance(self, qualified_name: str) -> str:
        return rel.get_node_type_for_inheritance(qualified_name, self.function_registry)
