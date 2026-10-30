"""Go's share of the class pass: the location index the go/types facts join
back through, and a struct's field types.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships) and hands Go's part to
the `GoClassIngest` it builds; a Go interface's method specs are the Go
interface pass's."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...utils.path_utils import cached_relative_path
from ..language_definitions import LanguageClassIngest
from .type_inference import GoTypeInferenceEngine

if TYPE_CHECKING:
    from ..definition_facts import DefinitionFacts


class GoClassIngest(LanguageClassIngest):
    """Records where each Go type is declared and what its struct fields are.

    Built and owned by `ClassIngest`. The definition facts and the location
    index are the pass's own, held by reference: the go/types join reads the
    index after Pass 2, and the graph updater's rehydration refills it."""

    __slots__ = ("repo_path", "facts", "type_locations")

    def __init__(
        self,
        *,
        repo_path: Path,
        facts: DefinitionFacts,
        type_locations: dict[tuple[str, int, int], tuple[str, str]],
    ) -> None:
        self.repo_path = repo_path
        self.facts = facts
        self.type_locations = type_locations

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

    def record_field_types(self, class_node: Node, class_qn: str) -> None:
        # Record Go struct field types so a field-hop receiver
        # (`engine.trees.get()`) resolves, and a local bound from such a call
        # (`root := engine.trees.get(m)`) picks up the return type.
        if field_types := GoTypeInferenceEngine().build_field_type_map(class_node):
            self.facts.class_field_types[class_qn] = field_types
