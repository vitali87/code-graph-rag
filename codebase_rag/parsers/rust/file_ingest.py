"""Rust's share of the file pass: what a file settles once its methods have
claimed their names, its held-back body-local items and the `use`s keyed on
a function's registered qn.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and hands Rust's
part to the `RustFileIngest` it builds; the body-local items are the
function pass's `RustFunctionIngest`, the `use`s the Rust import pass's."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFileIngest

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries
    from ..definition_facts import DefinitionFacts
    from ..import_processor import ImportProcessor
    from .function_ingest import RustFunctionIngest


class RustFileIngest(LanguageFileIngest):
    """Settles a Rust file's body-local items and function-scope `use`s once
    its function and class passes have run.

    Built and owned by `DefinitionProcessor`. The function pass's
    `RustFunctionIngest`, the import processor and the definition facts are
    the pass's own, held by reference."""

    __slots__ = ("_function_ingest", "import_processor", "facts")

    def __init__(
        self,
        function_ingest: RustFunctionIngest,
        import_processor: ImportProcessor,
        facts: DefinitionFacts,
    ) -> None:
        self._function_ingest = function_ingest
        self.import_processor = import_processor
        self.facts = facts

    def finalise(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        # The methods above have claimed their names; a body-local item
        # may now take what is left of the one it shares.
        self._function_ingest.flush_deferred_body_local()
        # Function-body uses key on the REGISTERED qn of their
        # enclosing function, knowable only now that dedup variants
        # (`natural@<line>`) have been handed out.
        self.import_processor.rust.finalise_rust_function_scope_uses(
            module_qn, self.facts.function_locations
        )
        # Block-local items key on their own registered qn, which
        # the same pass has just handed out.
        self.import_processor.rust.record_rust_block_items(
            module_qn, root_node, self.facts.function_locations
        )
        # Inline-mod maps were visible to this file's own impl
        # ingestion above; retract them until the flush-time
        # arbitration decides key ownership across all files.
        self.import_processor.rust.retract_rust_mod_scope_uses(module_qn)
