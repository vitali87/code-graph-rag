"""Go's share of the file pass: the package a file's `package` clause puts
it in.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and hands Go's part
to the `GoFileIngest` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_sitter import Node

from ..language_definitions import LanguageFileIngest
from . import utils as go_utils

if TYPE_CHECKING:
    from ..definition_facts import DefinitionFacts


class GoFileIngest(LanguageFileIngest):
    """Records the package each Go file declares.

    Built and owned by `DefinitionProcessor`. The definition facts are the
    pass's own, held by reference."""

    __slots__ = ("facts",)

    def __init__(self, facts: DefinitionFacts) -> None:
        self.facts = facts

    def record_module_facts(self, module_qn: str, root_node: Node) -> None:
        if package_name := go_utils.extract_package_name(root_node):
            self.facts.go_package_names[module_qn] = package_name
