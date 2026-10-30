"""Python's share of the file pass: the plain names a file's classes reserve
before its functions register, and the `@overload` stubs that fold into
their implementation.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and hands Python's
part to the `PythonFileIngest` it builds."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFileIngest
from .overloads import folded_overload_stubs

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries
    from .class_ingest import PythonClassIngest


class PythonFileIngest(LanguageFileIngest):
    """Reserves a Python file's class names and folds its overload stubs.

    Built and owned by `DefinitionProcessor`. The reservation is the class
    pass's `PythonClassIngest`, held by reference."""

    __slots__ = ("_class_ingest",)

    def __init__(self, class_ingest: PythonClassIngest) -> None:
        self._class_ingest = class_ingest

    def reserve_names(
        self,
        combined_captures: dict[str, list] | None,
        module_qn: str,
        lang_queries: LanguageQueries,
        file_path: Path,
    ) -> None:
        self._class_ingest.reserve_class_qns(
            combined_captures, module_qn, lang_queries[cs.QUERY_CONFIG], file_path
        )

    def folded_overloads(
        self,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None,
    ) -> frozenset[int] | None:
        return folded_overload_stubs(root_node)
