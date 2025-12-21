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
from .cpp_utils import (
    build_cpp_qualified_name,
    extract_cpp_exported_class_name,
    is_cpp_exported,
)
from .java_utils import extract_java_method_info
from .python_utils import resolve_class_name
from .rust_utils import build_rust_module_path, extract_rust_impl_target
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
                has_module = False

                for child in node.children:
                    if child.type == cs.ONEOF_MODULE or (
                        child.text
                        and safe_decode_with_fallback(child).strip() == cs.ONEOF_MODULE
                    ):
                        has_module = True

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

                if (
                    node_text.startswith("export class ")
                    or node_text.startswith("export struct ")
                    or node_text.startswith("export template")
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
        query = lang_queries[cs.QUERY_CLASSES]
        if not query:
            return

        lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]

        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        class_nodes = captures.get(cs.CAPTURE_CLASS, [])
        module_nodes = captures.get(cs.ONEOF_MODULE, [])

        if language == cs.SupportedLanguage.CPP:
            additional_class_nodes = self._find_cpp_exported_classes(root_node)
            class_nodes.extend(additional_class_nodes)

        file_path = self.module_qn_to_file_path.get(module_qn)

        for class_node in class_nodes:
            if not isinstance(class_node, Node):
                continue

            class_qn = None
            class_name = None
            is_exported = False

            if (
                language == cs.SupportedLanguage.RUST
                and class_node.type == cs.TS_IMPL_ITEM
            ):
                impl_target = extract_rust_impl_target(class_node)
                if not impl_target:
                    continue

                class_qn = f"{module_qn}.{impl_target}"

                body_node = class_node.child_by_field_name("body")
                method_query = lang_queries[cs.QUERY_FUNCTIONS]
                if body_node and method_query:
                    method_cursor = QueryCursor(method_query)
                    method_captures = method_cursor.captures(body_node)
                    method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])
                    for method_node in method_nodes:
                        if not isinstance(method_node, Node):
                            continue

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

                continue

            fqn_config = LANGUAGE_FQN_SPECS.get(language)
            if fqn_config and file_path:
                class_qn = resolve_fqn_from_ast(
                    class_node, file_path, self.repo_path, self.project_name, fqn_config
                )
                if class_qn:
                    class_name = class_qn.split(cs.SEPARATOR_DOT)[-1]
                    if language == cs.SupportedLanguage.CPP:
                        if class_node.type == cs.CppNodeType.FUNCTION_DEFINITION:
                            is_exported = True
                        else:
                            is_exported = is_cpp_exported(class_node)

            if not class_qn:
                if language == cs.SupportedLanguage.CPP:
                    if class_node.type == cs.CppNodeType.FUNCTION_DEFINITION:
                        class_name = extract_cpp_exported_class_name(class_node)
                        is_exported = True
                    else:
                        class_name = self._extract_cpp_class_name(class_node)
                        is_exported = is_cpp_exported(class_node)

                    if not class_name:
                        continue
                    class_qn = build_cpp_qualified_name(
                        class_node, module_qn, class_name
                    )
                else:
                    is_exported = False
                    class_name = self._extract_class_name(class_node)
                    if not class_name:
                        continue
                    nested_qn = self._build_nested_qualified_name_for_class(
                        class_node, module_qn, class_name, lang_config
                    )
                    class_qn = nested_qn or f"{module_qn}.{class_name}"

            decorators = self._extract_decorators(class_node)
            class_props: dict[str, Any] = {
                "qualified_name": class_qn,
                "name": class_name,
                "decorators": decorators,
                "start_line": class_node.start_point[0] + 1,
                "end_line": class_node.end_point[0] + 1,
                "docstring": self._get_docstring(class_node),
                "is_exported": is_exported,
            }
            match class_node.type:
                case cs.TS_INTERFACE_DECLARATION:
                    node_type = NodeType.INTERFACE
                    logger.info(
                        logs.CLASS_FOUND_INTERFACE.format(name=class_name, qn=class_qn)
                    )
                case (
                    cs.TS_ENUM_DECLARATION
                    | cs.TS_ENUM_SPECIFIER
                    | cs.TS_ENUM_CLASS_SPECIFIER
                ):
                    node_type = NodeType.ENUM
                    logger.info(
                        logs.CLASS_FOUND_ENUM.format(name=class_name, qn=class_qn)
                    )
                case cs.TS_TYPE_ALIAS_DECLARATION:
                    node_type = NodeType.TYPE
                    logger.info(
                        logs.CLASS_FOUND_TYPE.format(name=class_name, qn=class_qn)
                    )
                case cs.TS_STRUCT_SPECIFIER:
                    node_type = NodeType.CLASS
                    logger.info(
                        logs.CLASS_FOUND_STRUCT.format(name=class_name, qn=class_qn)
                    )
                case cs.TS_UNION_SPECIFIER:
                    node_type = NodeType.UNION
                    logger.info(
                        logs.CLASS_FOUND_UNION.format(name=class_name, qn=class_qn)
                    )
                case cs.CppNodeType.TEMPLATE_DECLARATION:
                    template_class = self._extract_template_class_type(class_node)
                    node_type = template_class or NodeType.CLASS
                    logger.info(
                        logs.CLASS_FOUND_TEMPLATE.format(
                            node_type=node_type, name=class_name, qn=class_qn
                        )
                    )
                case cs.CppNodeType.FUNCTION_DEFINITION if (
                    language == cs.SupportedLanguage.CPP
                ):
                    node_text = (
                        safe_decode_with_fallback(class_node) if class_node.text else ""
                    )
                    if "export struct " in node_text:
                        logger.info(
                            logs.CLASS_FOUND_EXPORTED_STRUCT.format(
                                name=class_name, qn=class_qn
                            )
                        )
                    elif "export union " in node_text:
                        logger.info(
                            logs.CLASS_FOUND_EXPORTED_UNION.format(
                                name=class_name, qn=class_qn
                            )
                        )
                    elif "export template" in node_text:
                        logger.info(
                            logs.CLASS_FOUND_EXPORTED_TEMPLATE.format(
                                name=class_name, qn=class_qn
                            )
                        )
                    else:
                        logger.info(
                            logs.CLASS_FOUND_EXPORTED_CLASS.format(
                                name=class_name, qn=class_qn
                            )
                        )
                    node_type = NodeType.CLASS
                case _:
                    node_type = NodeType.CLASS
                    logger.info(
                        logs.CLASS_FOUND_CLASS.format(name=class_name, qn=class_qn)
                    )

            self.ingestor.ensure_node_batch(node_type, class_props)

            self.function_registry[class_qn] = node_type
            if class_name:
                self.simple_name_lookup[class_name].add(class_qn)

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
                self._create_inheritance_relationship(
                    node_type, class_qn, parent_class_qn
                )

            if class_node.type == cs.TS_CLASS_DECLARATION:
                implemented_interfaces = self._extract_implemented_interfaces(
                    class_node, module_qn
                )
                for interface_qn in implemented_interfaces:
                    self._create_implements_relationship(
                        node_type, class_qn, interface_qn
                    )

            body_node = class_node.child_by_field_name("body")
            method_query = lang_queries[cs.QUERY_FUNCTIONS]
            if not body_node or not method_query:
                continue

            method_cursor = QueryCursor(method_query)
            method_captures = method_cursor.captures(body_node)
            method_nodes_list = method_captures.get(cs.CAPTURE_FUNCTION, [])
            for method_node in method_nodes_list:
                if not isinstance(method_node, Node):
                    continue

                method_qualified_name = None
                if language == cs.SupportedLanguage.JAVA:
                    method_info = extract_java_method_info(method_node)
                    method_name = method_info.get(cs.KEY_NAME)
                    parameters = method_info.get("parameters", [])
                    if method_name:
                        if parameters:
                            param_signature = "(" + ",".join(parameters) + ")"
                            method_qualified_name = (
                                f"{class_qn}.{method_name}{param_signature}"
                            )
                        else:
                            method_qualified_name = f"{class_qn}.{method_name}()"

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

        for module_node in module_nodes:
            if not isinstance(module_node, Node):
                continue

            module_name_node = module_node.child_by_field_name("name")
            if not module_name_node:
                continue
            text = module_name_node.text
            if text is None:
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

        path_parts = build_rust_module_path(
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

        for base_child in base_clause_node.children:
            parent_name = None

            if base_child.type == cs.TS_TYPE_IDENTIFIER:
                if base_child.text:
                    parent_name = safe_decode_text(base_child)

            elif base_child.type == cs.CppNodeType.QUALIFIED_IDENTIFIER:
                if base_child.text:
                    parent_name = safe_decode_text(base_child)

            elif base_child.type == cs.TS_TEMPLATE_TYPE:
                if base_child.text:
                    parent_name = safe_decode_text(base_child)

            elif base_child.type in [cs.TS_ACCESS_SPECIFIER, cs.TS_VIRTUAL, ",", ":"]:
                continue

            if parent_name:
                base_name = self._extract_cpp_base_class_name(parent_name)
                parent_qn = build_cpp_qualified_name(class_node, module_qn, base_name)
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

    def _extract_parent_classes(self, class_node: Node, module_qn: str) -> list[str]:
        parent_classes: list[str] = []

        if class_node.type in cs.CPP_CLASS_TYPES:
            for child in class_node.children:
                if child.type == cs.TS_BASE_CLASS_CLAUSE:
                    parent_classes.extend(
                        self._parse_cpp_base_classes(child, class_node, module_qn)
                    )
            return parent_classes

        if class_node.type == cs.TS_CLASS_DECLARATION:
            if superclass_node := class_node.child_by_field_name("superclass"):
                if superclass_node.type == cs.TS_TYPE_IDENTIFIER:
                    if (
                        resolved_superclass
                        := self._resolve_superclass_from_type_identifier(
                            superclass_node, module_qn
                        )
                    ):
                        parent_classes.append(resolved_superclass)
                else:
                    for child in superclass_node.children:
                        if child.type == cs.TS_TYPE_IDENTIFIER:
                            if resolved_superclass := (
                                self._resolve_superclass_from_type_identifier(
                                    child, module_qn
                                )
                            ):
                                parent_classes.append(resolved_superclass)
                                break

        if superclasses_node := class_node.child_by_field_name("superclasses"):
            for child in superclasses_node.children:
                if child.type == cs.TS_IDENTIFIER:
                    parent_text = child.text
                    if parent_text:
                        parent_name = safe_decode_text(child)
                        if not parent_name:
                            continue
                        if module_qn in self.import_processor.import_mapping:
                            import_map = self.import_processor.import_mapping[module_qn]
                            if parent_name in import_map:
                                parent_classes.append(import_map[parent_name])
                            else:
                                parent_classes.append(
                                    self._resolve_to_qn(parent_name, module_qn)
                                )
                        else:
                            parent_classes.append(f"{module_qn}.{parent_name}")

        if class_heritage_node := _find_child_by_type(class_node, cs.TS_CLASS_HERITAGE):
            for child in class_heritage_node.children:
                if child.type == cs.TS_EXTENDS_CLAUSE:
                    for grandchild in child.children:
                        if grandchild.type in cs.JS_TS_PARENT_REF_TYPES:
                            if parent_text := grandchild.text:
                                parent_name = parent_text.decode(cs.ENCODING_UTF8)
                                parent_classes.append(
                                    self._resolve_js_ts_parent_class(
                                        parent_name, module_qn
                                    )
                                )
                            break
                    break
                elif child.type in cs.JS_TS_PARENT_REF_TYPES:
                    child_index = class_heritage_node.children.index(child)
                    if (
                        child_index > 0
                        and class_heritage_node.children[child_index - 1].type
                        == cs.TS_EXTENDS
                    ):
                        parent_text = child.text
                        if parent_text:
                            if parent_name := safe_decode_text(child):
                                parent_classes.append(
                                    self._resolve_js_ts_parent_class(
                                        parent_name, module_qn
                                    )
                                )
                elif child.type == cs.TS_CALL_EXPRESSION:
                    child_index = class_heritage_node.children.index(child)
                    if (
                        child_index > 0
                        and class_heritage_node.children[child_index - 1].type
                        == cs.TS_EXTENDS
                    ):
                        parent_classes.extend(
                            self._extract_mixin_parent_classes(child, module_qn)
                        )

        if class_node.type == cs.TS_INTERFACE_DECLARATION:
            if extends_type_clause_node := _find_child_by_type(
                class_node, cs.TS_EXTENDS_TYPE_CLAUSE
            ):
                for child in extends_type_clause_node.children:
                    if parent_text := child.text:
                        if child.type == cs.TS_TYPE_IDENTIFIER:
                            if parent_name := safe_decode_text(child):
                                parent_classes.append(
                                    self._resolve_js_ts_parent_class(
                                        parent_name, module_qn
                                    )
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
