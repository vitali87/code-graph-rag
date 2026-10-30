"""Dart's share of the function pass: a free function's return type, and
the `main` a library declares as the program's entry.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
Dart's part to the `DartFunctionIngest` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_sitter import Node

from ..language_definitions import LanguageFunctionIngest
from . import utils as dart_utils

if TYPE_CHECKING:
    from ..definition_facts import DefinitionFacts


class DartFunctionIngest(LanguageFunctionIngest):
    """Records what a Dart free function declares beside the shared function
    pass.

    Built and owned by `FunctionIngest`. The definition facts are the pass's
    own, held by reference."""

    __slots__ = ("facts",)

    def __init__(self, facts: DefinitionFacts) -> None:
        self.facts = facts

    def is_program_main(self, func_node: Node) -> bool:
        return dart_utils.is_top_level_main(func_node)

    def record_return_type(self, func_node: Node, qualified_name: str) -> None:
        # A Dart free function's return type: a call-bound local
        # (`var g = makeIt()` via the enrichment pass) types from
        # this map.
        if return_type := dart_utils.return_type_name(func_node):
            self.facts.method_return_types[qualified_name] = return_type
