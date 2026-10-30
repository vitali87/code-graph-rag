"""C++'s share of the file pass: the C++20 module unit a file declares, and
the type aliases its `typedef`s and `using`s name.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and hands C++'s
part to the `CppFileIngest` it builds; the module units are the class
pass's `CppClassIngest`."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFileIngest
from .type_inference import CppTypeInferenceEngine

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries
    from ..definition_facts import DefinitionFacts
    from .class_ingest import CppClassIngest


class CppFileIngest(LanguageFileIngest):
    """Records a C++ file's module unit and the aliases it declares.

    Built and owned by `DefinitionProcessor`. The class pass's
    `CppClassIngest` and the definition facts are the pass's own, held by
    reference."""

    __slots__ = ("_class_ingest", "facts", "_type_alias_conflicts")

    def __init__(self, class_ingest: CppClassIngest, facts: DefinitionFacts) -> None:
        self._class_ingest = class_ingest
        self.facts = facts
        # Alias names seen with conflicting underlying types across
        # scopes/files; dropped from type_aliases so their receivers fall
        # back to name-only.
        self._type_alias_conflicts: set[str] = set()

    def ingest_after_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        file_path: Path,
    ) -> None:
        self._class_ingest.ingest_module_declarations(root_node, module_qn, file_path)
        CppTypeInferenceEngine().collect_type_aliases(
            root_node, self.facts.type_aliases, self._type_alias_conflicts
        )
