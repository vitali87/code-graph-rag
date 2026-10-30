"""Python's share of the function pass: the `@overload` stubs it leaves to
the implementation they fold into, and the positional parameter names a
call into a function is counted against.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
Python's part to the `PythonFunctionIngest` it builds; the stubs are the
ones the definition pass folds per module."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ..callable_params import python_positional_parameter_names
from ..language_definitions import LanguageFunctionIngest

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import LanguageQueries


class PythonFunctionIngest(LanguageFunctionIngest):
    """Skips the overload stubs a Python module folds, and reads a Python
    function's positional parameters.

    Built and owned by `FunctionIngest`. The folded overloads are the pass's
    own, held by reference: the definition pass fills them per module."""

    __slots__ = ("folded_overloads",)

    def __init__(self, folded_overloads: dict[str, frozenset[int]]) -> None:
        self.folded_overloads = folded_overloads

    def skips_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        # The implementation after an `@overload` stub owns the function's
        # name; the stub is no function of its own (issue #2590).
        return func_node.start_byte in self.folded_overloads.get(module_qn, frozenset())

    def positional_params(
        self, func_node: Node, language: cs.SupportedLanguage
    ) -> list[str] | None:
        # CPython's positional names rather than the declared optionality the
        # other languages store (issue #2517).
        return python_positional_parameter_names(func_node)
