"""Scala's share of the class pass: the base a class's imports name, which
outranks the class parse time bound the written name to.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships, and the deferred
INHERITS/IMPLEMENTS resolution) and hands Scala's part to the
`ScalaClassIngest` it builds; the imports themselves are the Scala import
pass's."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..language_definitions import LanguageClassIngest

if TYPE_CHECKING:
    from ...types_defs import DeferredInherit
    from ..import_processor import ImportProcessor


class ScalaClassIngest(LanguageClassIngest):
    """Binds a held-back Scala base through the imports around its class.

    Built and owned by `ClassIngest`. The import processor is the pass's
    own, held by reference."""

    __slots__ = ("import_processor",)

    def __init__(self, import_processor: ImportProcessor) -> None:
        self.import_processor = import_processor

    def imported_base(self, entry: DeferredInherit) -> str | None:
        # Ahead of the registry check: parse time bound the base by name
        # alone when only a package block imports it, and a sibling block's
        # same-named class can be what that found.
        return self.import_processor.scala.scala_base_qn(
            entry.module_qn, entry.child_qn, entry.parent_qn, entry.base_index
        )
