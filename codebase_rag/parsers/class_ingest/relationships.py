from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import NodeType
from . import parent_extraction as pe

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol
    from ..import_processor import ImportProcessor


def create_class_relationships(
    class_node: Node,
    class_qn: str,
    module_qn: str,
    node_type: NodeType,
    is_exported: bool,
    language: cs.SupportedLanguage,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
    function_registry: FunctionRegistryTrieProtocol,
) -> None:
    parent_classes = pe.extract_parent_classes(
        class_node, module_qn, import_processor, resolve_to_qn
    )
    class_inheritance[class_qn] = parent_classes

    ingestor.ensure_relationship_batch(
        (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
        cs.RelationshipType.DEFINES,
        (node_type, cs.KEY_QUALIFIED_NAME, class_qn),
    )

    if is_exported and language == cs.SupportedLanguage.CPP:
        ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            cs.RelationshipType.EXPORTS,
            (node_type, cs.KEY_QUALIFIED_NAME, class_qn),
        )

    for parent_class_qn in parent_classes:
        create_inheritance_relationship(
            node_type, class_qn, parent_class_qn, function_registry, ingestor
        )

    if class_node.type == cs.TS_CLASS_DECLARATION:
        for interface_qn in pe.extract_implemented_interfaces(
            class_node, module_qn, resolve_to_qn
        ):
            create_implements_relationship(node_type, class_qn, interface_qn, ingestor)


def get_node_type_for_inheritance(
    qualified_name: str,
    function_registry: FunctionRegistryTrieProtocol,
) -> str:
    node_type = function_registry.get(qualified_name, NodeType.CLASS)
    return str(node_type)


def create_inheritance_relationship(
    child_node_type: str,
    child_qn: str,
    parent_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    ingestor: IngestorProtocol,
) -> None:
    parent_type = get_node_type_for_inheritance(parent_qn, function_registry)
    ingestor.ensure_relationship_batch(
        (child_node_type, cs.KEY_QUALIFIED_NAME, child_qn),
        cs.RelationshipType.INHERITS,
        (parent_type, cs.KEY_QUALIFIED_NAME, parent_qn),
    )


def create_implements_relationship(
    class_type: str,
    class_qn: str,
    interface_qn: str,
    ingestor: IngestorProtocol,
) -> None:
    ingestor.ensure_relationship_batch(
        (class_type, cs.KEY_QUALIFIED_NAME, class_qn),
        cs.RelationshipType.IMPLEMENTS,
        (cs.NodeLabel.INTERFACE, cs.KEY_QUALIFIED_NAME, interface_qn),
    )
