from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs
from ..language_spec import LANGUAGE_FQN_SPECS
from ..types_defs import NodeType
from ..utils.fqn_resolver import resolve_fqn_from_ast
from .cpp import utils as cpp_utils
from .java import utils as java_utils
from .py import resolve_class_name
from .rs import utils as rs_utils
from .utils import ingest_method, safe_decode_text, safe_decode_with_fallback

if TYPE_CHECKING:
    from ..language_spec import LanguageSpec
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .import_processor import ImportProcessor


def _decode_node_stripped(node: Node) -> str:
    return safe_decode_with_fallback(node).strip() if node.text else ""


def _find_child_by_type(node: Node, node_type: str) -> Node | None:
    return next((c for c in node.children if c.type == node_type), None)


class ClassIngestMixin:
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: Any
    simple_name_lookup: Any
    module_qn_to_file_path: dict[str, Path]
    import_processor: ImportProcessor
    class_inheritance: dict[str, list[str]]
    _get_docstring: Callable[[Node], str | None]
    _extract_decorators: Callable[[Node], list[str]]

    def _resolve_to_qn(self, name: str, module_qn: str) -> str:
        return self._resolve_class_name(name, module_qn) or f"{module_qn}.{name}"

    def _ingest_cpp_module_declarations(
        self,
        root_node: Node,
        module_qn: str,
        file_path: Path,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        module_declarations: list[tuple[Node, str]] = []

        def find_module_declarations(node: Node) -> None:
            if node.type == cs.TS_MODULE_DECLARATION:
                module_declarations.append((node, _decode_node_stripped(node)))

            elif node.type == cs.CppNodeType.DECLARATION:
                has_module = any(
                    child.type == cs.ONEOF_MODULE
                    or (
                        child.text
                        and safe_decode_with_fallback(child).strip() == cs.ONEOF_MODULE
                    )
                    for child in node.children
                )
                if has_module:
                    module_declarations.append((node, _decode_node_stripped(node)))

            for child in node.children:
                find_module_declarations(child)

        find_module_declarations(root_node)

        for decl_node, decl_text in module_declarations:
            if decl_text.startswith("export module "):
                parts = decl_text.split()
                if len(parts) >= 3:
                    module_name = parts[2].rstrip(";")

                    interface_qn = f"{self.project_name}.{module_name}"
                    self.ingestor.ensure_node_batch(
                        cs.NodeLabel.MODULE_INTERFACE,
                        {
                            cs.KEY_QUALIFIED_NAME: interface_qn,
                            cs.KEY_NAME: module_name,
                            cs.KEY_PATH: str(file_path.relative_to(self.repo_path)),
                            "module_type": "interface",
                        },
                    )

                    self.ingestor.ensure_relationship_batch(
                        (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                        cs.RelationshipType.EXPORTS_MODULE,
                        (
                            cs.NodeLabel.MODULE_INTERFACE,
                            cs.KEY_QUALIFIED_NAME,
                            interface_qn,
                        ),
                    )

                    logger.info(logs.CLASS_CPP_MODULE_INTERFACE.format(qn=interface_qn))

            elif decl_text.startswith("module ") and not decl_text.startswith(
                "module ;"
            ):
                parts = decl_text.split()
                if len(parts) >= 2:
                    module_name = parts[1].rstrip(";")

                    impl_qn = f"{self.project_name}.{module_name}_impl"
                    self.ingestor.ensure_node_batch(
                        cs.NodeLabel.MODULE_IMPLEMENTATION,
                        {
                            cs.KEY_QUALIFIED_NAME: impl_qn,
                            cs.KEY_NAME: f"{module_name}_impl",
                            cs.KEY_PATH: str(file_path.relative_to(self.repo_path)),
                            "implements_module": module_name,
                            "module_type": "implementation",
                        },
                    )

                    self.ingestor.ensure_relationship_batch(
                        (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                        cs.RelationshipType.IMPLEMENTS_MODULE,
                        (
                            cs.NodeLabel.MODULE_IMPLEMENTATION,
                            cs.KEY_QUALIFIED_NAME,
                            impl_qn,
                        ),
                    )

                    interface_qn = f"{self.project_name}.{module_name}"
                    self.ingestor.ensure_relationship_batch(
                        (
                            cs.NodeLabel.MODULE_IMPLEMENTATION,
                            cs.KEY_QUALIFIED_NAME,
                            impl_qn,
                        ),
                        cs.RelationshipType.IMPLEMENTS,
                        (
                            cs.NodeLabel.MODULE_INTERFACE,
                            cs.KEY_QUALIFIED_NAME,
                            interface_qn,
                        ),
                    )

                    logger.info(logs.CLASS_CPP_MODULE_IMPL.format(qn=impl_qn))

    def _find_cpp_exported_classes(self, root_node: Node) -> list[Node]:
        exported_class_nodes: list[Node] = []

        def traverse_for_exported_classes(node: Node) -> None:
            if node.type == cs.CppNodeType.FUNCTION_DEFINITION:
                node_text = _decode_node_stripped(node)

                if node_text.startswith(
                    ("export class ", "export struct ", "export template")
                ):
                    for child in node.children:
                        if child.type == cs.TS_ERROR and child.text:
                            error_text = safe_decode_text(child)
                            if error_text in ["class", "struct"]:
                                exported_class_nodes.append(node)
                                break
                    else:
                        if (
                            "export class " in node_text
                            or "export struct " in node_text
                        ):
                            exported_class_nodes.append(node)

            for child in node.children:
                traverse_for_exported_classes(child)

        traverse_for_exported_classes(root_node)
        return exported_class_nodes

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

        identity = self._resolve_class_identity(
            class_node, module_qn, language, lang_config, file_path
        )
        if not identity:
            return

        class_qn, class_name, is_exported = identity
        node_type = self._determine_node_type(
            class_node, class_name, class_qn, language
        )

        class_props: dict[str, Any] = {
            "qualified_name": class_qn,
            "name": class_name,
            "decorators": self._extract_decorators(class_node),
            "start_line": class_node.start_point[0] + 1,
            "end_line": class_node.end_point[0] + 1,
            "docstring": self._get_docstring(class_node),
            "is_exported": is_exported,
        }
        self.ingestor.ensure_node_batch(node_type, class_props)
        self.function_registry[class_qn] = node_type
        if class_name:
            self.simple_name_lookup[class_name].add(class_qn)

        self._create_class_relationships(
            class_node, class_qn, module_qn, node_type, is_exported, language
        )
        self._ingest_class_methods(class_node, class_qn, language, lang_queries)

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
                ingest_method(
                    method_node,
                    class_qn,
                    cs.NodeLabel.CLASS,
                    self.ingestor,
                    self.function_registry,
                    self.simple_name_lookup,
                    self._get_docstring,
                    language,
                )

    def _resolve_class_identity(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> tuple[str, str, bool] | None:
        if (fqn_config := LANGUAGE_FQN_SPECS.get(language)) and file_path:
            if class_qn := resolve_fqn_from_ast(
                class_node,
                file_path,
                self.repo_path,
                self.project_name,
                fqn_config,
            ):
                class_name = class_qn.split(cs.SEPARATOR_DOT)[-1]
                is_exported = language == cs.SupportedLanguage.CPP and (
                    class_node.type == cs.CppNodeType.FUNCTION_DEFINITION
                    or cpp_utils.is_exported(class_node)
                )
                return class_qn, class_name, is_exported

        return self._resolve_class_identity_fallback(
            class_node, module_qn, language, lang_config
        )

    def _resolve_class_identity_fallback(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> tuple[str, str, bool] | None:
        if language == cs.SupportedLanguage.CPP:
            if class_node.type == cs.CppNodeType.FUNCTION_DEFINITION:
                class_name = cpp_utils.extract_exported_class_name(class_node)
                is_exported = True
            else:
                class_name = self._extract_cpp_class_name(class_node)
                is_exported = cpp_utils.is_exported(class_node)

            if not class_name:
                return None
            class_qn = cpp_utils.build_qualified_name(class_node, module_qn, class_name)
            return class_qn, class_name, is_exported

        class_name = self._extract_class_name(class_node)
        if not class_name:
            return None
        nested_qn = self._build_nested_qualified_name_for_class(
            class_node, module_qn, class_name, lang_config
        )
        return nested_qn or f"{module_qn}.{class_name}", class_name, False

    def _determine_node_type(
        self,
        class_node: Node,
        class_name: str | None,
        class_qn: str,
        language: cs.SupportedLanguage,
    ) -> NodeType:
        match class_node.type:
            case cs.TS_INTERFACE_DECLARATION:
                logger.info(
                    logs.CLASS_FOUND_INTERFACE.format(name=class_name, qn=class_qn)
                )
                return NodeType.INTERFACE
            case (
                cs.TS_ENUM_DECLARATION
                | cs.TS_ENUM_SPECIFIER
                | cs.TS_ENUM_CLASS_SPECIFIER
            ):
                logger.info(logs.CLASS_FOUND_ENUM.format(name=class_name, qn=class_qn))
                return NodeType.ENUM
            case cs.TS_TYPE_ALIAS_DECLARATION:
                logger.info(logs.CLASS_FOUND_TYPE.format(name=class_name, qn=class_qn))
                return NodeType.TYPE
            case cs.TS_STRUCT_SPECIFIER:
                logger.info(
                    logs.CLASS_FOUND_STRUCT.format(name=class_name, qn=class_qn)
                )
                return NodeType.CLASS
            case cs.TS_UNION_SPECIFIER:
                logger.info(logs.CLASS_FOUND_UNION.format(name=class_name, qn=class_qn))
                return NodeType.UNION
            case cs.CppNodeType.TEMPLATE_DECLARATION:
                node_type = (
                    self._extract_template_class_type(class_node) or NodeType.CLASS
                )
                logger.info(
                    logs.CLASS_FOUND_TEMPLATE.format(
                        node_type=node_type, name=class_name, qn=class_qn
                    )
                )
                return node_type
            case cs.CppNodeType.FUNCTION_DEFINITION if (
                language == cs.SupportedLanguage.CPP
            ):
                self._log_exported_class_type(class_node, class_name, class_qn)
                return NodeType.CLASS
            case _:
                logger.info(logs.CLASS_FOUND_CLASS.format(name=class_name, qn=class_qn))
                return NodeType.CLASS

    def _log_exported_class_type(
        self, class_node: Node, class_name: str | None, class_qn: str
    ) -> None:
        node_text = safe_decode_with_fallback(class_node) if class_node.text else ""
        if "export struct " in node_text:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_STRUCT.format(name=class_name, qn=class_qn)
            )
        elif "export union " in node_text:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_UNION.format(name=class_name, qn=class_qn)
            )
        elif "export template" in node_text:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_TEMPLATE.format(name=class_name, qn=class_qn)
            )
        else:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_CLASS.format(name=class_name, qn=class_qn)
            )

    def _create_class_relationships(
        self,
        class_node: Node,
        class_qn: str,
        module_qn: str,
        node_type: NodeType,
        is_exported: bool,
        language: cs.SupportedLanguage,
    ) -> None:
        parent_classes = self._extract_parent_classes(class_node, module_qn)
        self.class_inheritance[class_qn] = parent_classes

        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            cs.RelationshipType.DEFINES,
            (node_type, cs.KEY_QUALIFIED_NAME, class_qn),
        )

        if is_exported and language == cs.SupportedLanguage.CPP:
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                cs.RelationshipType.EXPORTS,
                (node_type, cs.KEY_QUALIFIED_NAME, class_qn),
            )

        for parent_class_qn in parent_classes:
            self._create_inheritance_relationship(node_type, class_qn, parent_class_qn)

        if class_node.type == cs.TS_CLASS_DECLARATION:
            for interface_qn in self._extract_implemented_interfaces(
                class_node, module_qn
            ):
                self._create_implements_relationship(node_type, class_qn, interface_qn)

    def _ingest_class_methods(
        self,
        class_node: Node,
        class_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
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
                    parameters = method_info.get("parameters", [])
                    param_sig = f"({','.join(parameters)})" if parameters else "()"
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
            nested_qn = self._build_nested_qualified_name_for_class(
                module_node, module_qn, module_name or "", lang_config
            )
            inline_module_qn = nested_qn or f"{module_qn}.{module_name}"

            module_props: dict[str, Any] = {
                cs.KEY_QUALIFIED_NAME: inline_module_qn,
                cs.KEY_NAME: module_name,
                cs.KEY_PATH: f"inline_module_{module_name}",
            }
            logger.info(
                logs.CLASS_FOUND_INLINE_MODULE.format(
                    name=module_name, qn=inline_module_qn
                )
            )
            self.ingestor.ensure_node_batch(cs.NodeLabel.MODULE, module_props)

    def process_all_method_overrides(self) -> None:
        logger.info(logs.CLASS_PASS_4)

        for method_qn in self.function_registry.keys():
            if (
                self.function_registry[method_qn] == NodeType.METHOD
                and cs.SEPARATOR_DOT in method_qn
            ):
                parts = method_qn.rsplit(cs.SEPARATOR_DOT, 1)
                if len(parts) == 2:
                    class_qn, method_name = parts
                    self._check_method_overrides(method_qn, method_name, class_qn)

    def _check_method_overrides(
        self, method_qn: str, method_name: str, class_qn: str
    ) -> None:
        if class_qn not in self.class_inheritance:
            return

        queue = deque([class_qn])
        visited = {class_qn}

        while queue:
            current_class = queue.popleft()

            if current_class != class_qn:
                parent_method_qn = f"{current_class}.{method_name}"

                if parent_method_qn in self.function_registry:
                    self.ingestor.ensure_relationship_batch(
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
                        cs.RelationshipType.OVERRIDES,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, parent_method_qn),
                    )
                    logger.debug(
                        logs.CLASS_METHOD_OVERRIDE.format(
                            method_qn=method_qn, parent_method_qn=parent_method_qn
                        )
                    )
                    return

            if current_class in self.class_inheritance:
                for parent_class_qn in self.class_inheritance[current_class]:
                    if parent_class_qn not in visited:
                        visited.add(parent_class_qn)
                        queue.append(parent_class_qn)

    def _extract_template_class_type(self, template_node: Node) -> NodeType | None:
        for child in template_node.children:
            if child.type in cs.CPP_CLASS_TYPES:
                return NodeType.CLASS
            elif child.type == cs.TS_ENUM_SPECIFIER:
                return NodeType.ENUM
            elif child.type == cs.TS_UNION_SPECIFIER:
                return NodeType.UNION
        return None

    def _extract_cpp_class_name(self, class_node: Node) -> str | None:
        if class_node.type == cs.CppNodeType.TEMPLATE_DECLARATION:
            for child in class_node.children:
                if child.type in cs.CPP_COMPOUND_TYPES:
                    return self._extract_cpp_class_name(child)

        for child in class_node.children:
            if child.type == cs.TS_TYPE_IDENTIFIER and child.text:
                return safe_decode_text(child)

        name_node = class_node.child_by_field_name(cs.KEY_NAME)
        return safe_decode_text(name_node) if name_node and name_node.text else None

    def _extract_class_name(self, class_node: Node) -> str | None:
        name_node = class_node.child_by_field_name(cs.KEY_NAME)
        if name_node and name_node.text:
            return safe_decode_text(name_node)

        current = class_node.parent
        while current:
            if current.type == cs.TS_VARIABLE_DECLARATOR:
                for child in current.children:
                    if child.type == cs.TS_IDENTIFIER and child.text:
                        return safe_decode_text(child)
            current = current.parent

        return None

    def _build_nested_qualified_name_for_class(
        self,
        class_node: Node,
        module_qn: str,
        class_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        if not isinstance(class_node.parent, Node):
            return None

        path_parts = rs_utils.build_module_path(
            class_node,
            include_classes=True,
            class_node_types=lang_config.class_node_types,
        )

        if path_parts:
            return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{class_name}"
        return None

    def _get_node_type_for_inheritance(self, qualified_name: str) -> str:
        node_type = self.function_registry.get(qualified_name, NodeType.CLASS)
        return str(node_type)

    def _create_inheritance_relationship(
        self, child_node_type: str, child_qn: str, parent_qn: str
    ) -> None:
        parent_type = self._get_node_type_for_inheritance(parent_qn)
        self.ingestor.ensure_relationship_batch(
            (child_node_type, cs.KEY_QUALIFIED_NAME, child_qn),
            cs.RelationshipType.INHERITS,
            (parent_type, cs.KEY_QUALIFIED_NAME, parent_qn),
        )

    def _parse_cpp_base_classes(
        self, base_clause_node: Node, class_node: Node, module_qn: str
    ) -> list[str]:
        parent_classes: list[str] = []
        base_type_nodes = (
            cs.TS_TYPE_IDENTIFIER,
            cs.CppNodeType.QUALIFIED_IDENTIFIER,
            cs.TS_TEMPLATE_TYPE,
        )

        for base_child in base_clause_node.children:
            if base_child.type in [cs.TS_ACCESS_SPECIFIER, cs.TS_VIRTUAL, ",", ":"]:
                continue

            if base_child.type in base_type_nodes and base_child.text:
                if parent_name := safe_decode_text(base_child):
                    base_name = self._extract_cpp_base_class_name(parent_name)
                    parent_qn = cpp_utils.build_qualified_name(
                        class_node, module_qn, base_name
                    )
                    parent_classes.append(parent_qn)
                    logger.debug(
                        logs.CLASS_CPP_INHERITANCE.format(
                            parent_name=parent_name, parent_qn=parent_qn
                        )
                    )

        return parent_classes

    def _extract_cpp_base_class_name(self, parent_text: str) -> str:
        if "<" in parent_text:
            parent_text = parent_text.split("<")[0]

        if "::" in parent_text:
            parent_text = parent_text.split("::")[-1]

        return parent_text

    def _resolve_superclass_from_type_identifier(
        self, type_identifier_node: Node, module_qn: str
    ) -> str | None:
        if type_identifier_node.text:
            if parent_name := safe_decode_text(type_identifier_node):
                return self._resolve_to_qn(parent_name, module_qn)
        return None

    def _extract_cpp_parent_classes(
        self, class_node: Node, module_qn: str
    ) -> list[str]:
        parent_classes: list[str] = []
        for child in class_node.children:
            if child.type == cs.TS_BASE_CLASS_CLAUSE:
                parent_classes.extend(
                    self._parse_cpp_base_classes(child, class_node, module_qn)
                )
        return parent_classes

    def _extract_java_superclass(self, class_node: Node, module_qn: str) -> list[str]:
        superclass_node = class_node.child_by_field_name("superclass")
        if not superclass_node:
            return []

        if superclass_node.type == cs.TS_TYPE_IDENTIFIER:
            if resolved := self._resolve_superclass_from_type_identifier(
                superclass_node, module_qn
            ):
                return [resolved]
            return []

        for child in superclass_node.children:
            if child.type == cs.TS_TYPE_IDENTIFIER:
                if resolved := self._resolve_superclass_from_type_identifier(
                    child, module_qn
                ):
                    return [resolved]
        return []

    def _extract_python_superclasses(
        self, class_node: Node, module_qn: str
    ) -> list[str]:
        superclasses_node = class_node.child_by_field_name("superclasses")
        if not superclasses_node:
            return []

        parent_classes: list[str] = []
        import_map = self.import_processor.import_mapping.get(module_qn)

        for child in superclasses_node.children:
            if child.type != cs.TS_IDENTIFIER or not child.text:
                continue
            if not (parent_name := safe_decode_text(child)):
                continue

            if import_map and parent_name in import_map:
                parent_classes.append(import_map[parent_name])
            elif import_map:
                parent_classes.append(self._resolve_to_qn(parent_name, module_qn))
            else:
                parent_classes.append(f"{module_qn}.{parent_name}")

        return parent_classes

    def _extract_js_ts_heritage_parents(
        self, class_heritage_node: Node, module_qn: str
    ) -> list[str]:
        parent_classes: list[str] = []

        for child in class_heritage_node.children:
            if child.type == cs.TS_EXTENDS_CLAUSE:
                parent_classes.extend(
                    self._extract_from_extends_clause(child, module_qn)
                )
                break
            if child.type in cs.JS_TS_PARENT_REF_TYPES:
                if self._is_preceded_by_extends(child, class_heritage_node):
                    if parent_name := safe_decode_text(child):
                        parent_classes.append(
                            self._resolve_js_ts_parent_class(parent_name, module_qn)
                        )
            elif child.type == cs.TS_CALL_EXPRESSION:
                if self._is_preceded_by_extends(child, class_heritage_node):
                    parent_classes.extend(
                        self._extract_mixin_parent_classes(child, module_qn)
                    )

        return parent_classes

    def _extract_from_extends_clause(
        self, extends_clause: Node, module_qn: str
    ) -> list[str]:
        for grandchild in extends_clause.children:
            if grandchild.type in cs.JS_TS_PARENT_REF_TYPES and grandchild.text:
                parent_name = grandchild.text.decode(cs.ENCODING_UTF8)
                return [self._resolve_js_ts_parent_class(parent_name, module_qn)]
        return []

    def _is_preceded_by_extends(self, child: Node, parent_node: Node) -> bool:
        child_index = parent_node.children.index(child)
        return (
            child_index > 0
            and parent_node.children[child_index - 1].type == cs.TS_EXTENDS
        )

    def _extract_interface_parents(self, class_node: Node, module_qn: str) -> list[str]:
        extends_clause = _find_child_by_type(class_node, cs.TS_EXTENDS_TYPE_CLAUSE)
        if not extends_clause:
            return []

        parent_classes: list[str] = []
        for child in extends_clause.children:
            if child.type == cs.TS_TYPE_IDENTIFIER and child.text:
                if parent_name := safe_decode_text(child):
                    parent_classes.append(
                        self._resolve_js_ts_parent_class(parent_name, module_qn)
                    )
        return parent_classes

    def _extract_parent_classes(self, class_node: Node, module_qn: str) -> list[str]:
        if class_node.type in cs.CPP_CLASS_TYPES:
            return self._extract_cpp_parent_classes(class_node, module_qn)

        parent_classes: list[str] = []

        if class_node.type == cs.TS_CLASS_DECLARATION:
            parent_classes.extend(self._extract_java_superclass(class_node, module_qn))

        parent_classes.extend(self._extract_python_superclasses(class_node, module_qn))

        if class_heritage_node := _find_child_by_type(class_node, cs.TS_CLASS_HERITAGE):
            parent_classes.extend(
                self._extract_js_ts_heritage_parents(class_heritage_node, module_qn)
            )

        if class_node.type == cs.TS_INTERFACE_DECLARATION:
            parent_classes.extend(
                self._extract_interface_parents(class_node, module_qn)
            )

        return parent_classes

    def _extract_mixin_parent_classes(
        self, call_expr_node: Node, module_qn: str
    ) -> list[str]:
        parent_classes: list[str] = []

        for child in call_expr_node.children:
            if child.type == cs.TS_ARGUMENTS:
                for arg_child in child.children:
                    if arg_child.type == cs.TS_IDENTIFIER and arg_child.text:
                        if parent_name := safe_decode_text(arg_child):
                            parent_classes.append(
                                self._resolve_js_ts_parent_class(parent_name, module_qn)
                            )
                    elif arg_child.type == cs.TS_CALL_EXPRESSION:
                        parent_classes.extend(
                            self._extract_mixin_parent_classes(arg_child, module_qn)
                        )
                break

        return parent_classes

    def _resolve_js_ts_parent_class(self, parent_name: str, module_qn: str) -> str:
        if module_qn not in self.import_processor.import_mapping:
            return f"{module_qn}.{parent_name}"
        import_map = self.import_processor.import_mapping[module_qn]
        if parent_name in import_map:
            return import_map[parent_name]
        return self._resolve_to_qn(parent_name, module_qn)

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def _extract_implemented_interfaces(
        self, class_node: Node, module_qn: str
    ) -> list[str]:
        implemented_interfaces: list[str] = []

        interfaces_node = class_node.child_by_field_name("interfaces")
        if interfaces_node:
            self._extract_java_interface_names(
                interfaces_node, implemented_interfaces, module_qn
            )

        return implemented_interfaces

    def _extract_java_interface_names(
        self, interfaces_node: Node, interface_list: list[str], module_qn: str
    ) -> None:
        for child in interfaces_node.children:
            if child.type == cs.TS_TYPE_LIST:
                for type_child in child.children:
                    if type_child.type == cs.TS_TYPE_IDENTIFIER and type_child.text:
                        if interface_name := safe_decode_text(type_child):
                            interface_list.append(
                                self._resolve_to_qn(interface_name, module_qn)
                            )

    def _create_implements_relationship(
        self, class_type: str, class_qn: str, interface_qn: str
    ) -> None:
        self.ingestor.ensure_relationship_batch(
            (class_type, cs.KEY_QUALIFIED_NAME, class_qn),
            cs.RelationshipType.IMPLEMENTS,
            (cs.NodeLabel.INTERFACE, cs.KEY_QUALIFIED_NAME, interface_qn),
        )
