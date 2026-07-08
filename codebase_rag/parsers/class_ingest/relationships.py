from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import DeferredCppInherit, NodeType
from ..cpp import utils as cpp_utils
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
    interface_implementers: dict[str, set[str]] | None = None,
    defer_cpp_inherits: list[DeferredCppInherit] | None = None,
) -> None:
    cpp_bases: list[tuple[str, str]] | None = None
    if class_node.type in cs.CPP_CLASS_TYPES:
        cpp_bases = pe.extract_cpp_parent_bases(class_node, module_qn)
        parent_classes = [guess for _, guess in cpp_bases]
    else:
        parent_classes = pe.extract_parent_classes(
            class_node, module_qn, import_processor, resolve_to_qn
        )
    class_inheritance[class_qn] = parent_classes

    # (H) The DEFINES containment edge is emitted by the caller via
    # (H) _emit_or_defer_defines, so a non-module parent is verified against
    # (H) the registry after all passes instead of risking a phantom endpoint.

    if is_exported and language == cs.SupportedLanguage.CPP:
        ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
            cs.RelationshipType.EXPORTS,
            (node_type, cs.KEY_QUALIFIED_NAME, class_qn),
        )

    if cpp_bases is not None and defer_cpp_inherits is not None:
        # (H) A C++ base often lives in another header, so its qn cannot resolve
        # (H) until every class is registered; hold the INHERITS edge back for
        # (H) resolve_deferred_cpp_inherits instead of emitting the module-anchored
        # (H) guess (a phantom the database drops for every cross-file base).
        namespace_path = cs.SEPARATOR_DOT.join(
            cpp_utils.extract_namespace_path(class_node)
        )
        for base_index, (written_name, guess_qn) in enumerate(cpp_bases):
            defer_cpp_inherits.append(
                DeferredCppInherit(
                    child_label=str(node_type),
                    child_qn=class_qn,
                    base_name=written_name,
                    guess_qn=guess_qn,
                    namespace_path=namespace_path,
                    base_index=base_index,
                )
            )
    else:
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
