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
    parent_label: str,
    parent_qn: str,
    node_type: NodeType,
    is_exported: bool,
    language: cs.SupportedLanguage,
    class_inheritance: dict[str, list[str]],
    ingestor: IngestorProtocol,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
    function_registry: FunctionRegistryTrieProtocol,
    interface_implementers: dict[str, set[str]] | None = None,
) -> None:
    parent_classes = pe.extract_parent_classes(
        class_node, module_qn, import_processor, resolve_to_qn
    )
    class_inheritance[class_qn] = parent_classes

    ingestor.ensure_relationship_batch(
        (parent_label, cs.KEY_QUALIFIED_NAME, parent_qn),
        cs.RelationshipType.DEFINES,
        (node_type, cs.KEY_QUALIFIED_NAME, class_qn),
    )

    if is_exported and language == cs.SupportedLanguage.CPP:
        ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            cs.RelationshipType.EXPORTS,
            (node_type, cs.KEY_QUALIFIED_NAME, class_qn),
        )

    for base_index, parent_class_qn in enumerate(parent_classes):
        create_inheritance_relationship(
            node_type,
            class_qn,
            parent_class_qn,
            function_registry,
            ingestor,
            base_index,
        )

    # (H) A class OR an enum can `implements` interfaces; both expose them via the
    # (H) `interfaces` field (a super_interfaces clause), so handle both.
    if class_node.type in (cs.TS_CLASS_DECLARATION, cs.TS_ENUM_DECLARATION):
        for interface_qn in pe.extract_implemented_interfaces(
            class_node, module_qn, resolve_to_qn
        ):
            create_implements_relationship(node_type, class_qn, interface_qn, ingestor)
            # (H) Record implementers so the resolver can dispatch an interface-typed
            # (H) call to the concrete method when the interface has exactly one impl.
            if interface_implementers is not None:
                interface_implementers.setdefault(interface_qn, set()).add(class_qn)


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
    base_index: int = 0,
) -> None:
    parent_type = get_node_type_for_inheritance(parent_qn, function_registry)
    # (H) Persist the base's position in the child's base list. An incremental run
    # (H) rehydrates class_inheritance from these edges; ordering by base_index
    # (H) restores the original source order, which method resolution (Pass 3) and
    # (H) override attribution (Pass 4) depend on for multiple inheritance.
    ingestor.ensure_relationship_batch(
        (child_node_type, cs.KEY_QUALIFIED_NAME, child_qn),
        cs.RelationshipType.INHERITS,
        (parent_type, cs.KEY_QUALIFIED_NAME, parent_qn),
        {cs.KEY_BASE_INDEX: base_index},
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
