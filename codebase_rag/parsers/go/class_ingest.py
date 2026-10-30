"""Go's share of the class pass: the location index the go/types facts join
back through, an interface's embeds and method specs, a struct's field
types, and the interfaces whose overrides the Go interface link writes.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships) and hands Go's part to
the `GoClassIngest` it builds; a Go interface's method specs are written by
the Go interface pass (`GoInterfaceIngest`), which `GoClassIngest` builds
and the definition processor runs the interface link through."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import PropertyDict
from ...utils.path_utils import cached_relative_path
from ..language_definitions import LanguageClassIngest
from ..method_ingest import MethodIngestHost
from . import interfaces as go_ifaces
from .interface_ingest import GoInterfaceIngest
from .type_inference import GoTypeInferenceEngine

if TYPE_CHECKING:
    from ..definition_context import DefinitionContext
    from .definition_state import GoDefinitionState


class GoClassIngest(LanguageClassIngest):
    """Records where each Go type is declared, what an interface embeds and
    declares, and what a struct's fields are.

    Built and owned by `ClassIngest`. The definition facts, the location
    index and the interfaces are the pass's own, held by reference: the
    go/types join reads the index after Pass 2, the graph updater's
    rehydration refills it, and the interface pass fills the interfaces as
    it writes their method specs."""

    __slots__ = (
        "repo_path",
        "facts",
        "type_locations",
        "interfaces",
        "interface_ingest",
    )

    # link_go_interfaces gives each implementer its OVERRIDES, one per
    # interface satisfied and labelled with the pair's confidence, from every
    # method set, read-back ones included (issue #2483). The override walk
    # would add an unlabelled copy, and a pair the graph still holds after an
    # edit took it away.
    joins_read_back_implementers = False

    def __init__(
        self,
        method_host: MethodIngestHost,
        context: DefinitionContext,
        state: GoDefinitionState,
    ) -> None:
        self.repo_path = context.repo_path
        self.facts = context.facts
        self.type_locations = state.type_locations
        self.interfaces = state.interfaces
        self.interface_ingest = GoInterfaceIngest(
            method_host=method_host, context=context, state=state
        )

    def record_type_props(
        self,
        class_node: Node,
        class_qn: str,
        class_name: str,
        label: cs.NodeLabel,
        class_props: PropertyDict,
    ) -> None:
        # Stored so an incremental run that does not re-parse the interface
        # still knows what its method set is built from.
        if label == cs.NodeLabel.INTERFACE:
            class_props.update(go_ifaces.embeds_props(class_node))

    def record_type_location(
        self,
        class_qn: str,
        label: cs.NodeLabel,
        file_path: Path,
        class_start_line: int,
        class_start_col: int,
    ) -> None:
        # Reverse index for the go/types frontend's implements facts: each
        # ImplementsPair end (a declaring identifier position) joins back to
        # this type's qn + node label after Pass 2. A Go type_spec's
        # start_point IS the name token, so the position matches directly --
        # no name alias (unlike Go funcs, keyed at the func keyword).
        rel_path = cached_relative_path(file_path, self.repo_path).as_posix()
        self.type_locations[(rel_path, class_start_line, class_start_col)] = (
            class_qn,
            str(label),
        )

    def record_members(
        self,
        class_node: Node,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        label: cs.NodeLabel,
        modifiers: list[str],
        file_path: Path | None,
    ) -> None:
        if label == cs.NodeLabel.INTERFACE:
            self.interface_ingest.ingest_interface_methods(
                class_node, class_qn, module_qn, file_path
            )
        # Record Go struct field types so a field-hop receiver
        # (`engine.trees.get()`) resolves, and a local bound from such a call
        # (`root := engine.trees.get(m)`) picks up the return type.
        if field_types := GoTypeInferenceEngine().build_field_type_map(class_node):
            self.facts.class_field_types[class_qn] = field_types

    def overrides_linked_apart(self) -> Collection[str]:
        # The interfaces link_go_interfaces links the overrides of, for the
        # reason above.
        return self.interfaces.keys()
