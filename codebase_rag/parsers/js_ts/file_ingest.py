"""JS/TS's share of the file pass: the import patterns the import query
misses and the specifiers that resolved nowhere, TypeScript's overload
signatures, and the named JS/TS passes a file runs once its functions and
classes are in.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and hands JS/TS's
part to the `JsTsFileIngest` it builds; the named passes are `JsTsIngest`
and `JsTsModuleSystem`."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFileIngest
from ..utils import get_function_captures
from .overloads import folded_overload_signatures

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import LanguageQueries
    from ..import_processor import ImportProcessor
    from .function_ingest import JsTsFunctionIngest
    from .ingest import JsTsIngest
    from .module_system import JsTsModuleSystem


class JsTsFileIngest(LanguageFileIngest):
    """Runs the JS/TS passes around a file's function and class passes, and
    folds TypeScript's overload signatures into their implementation.

    Built and owned by `DefinitionProcessor`. The ingestor, the import
    processor, the named JS/TS passes and the function pass's
    `JsTsFunctionIngest` are the pass's own, held by reference."""

    __slots__ = (
        "ingestor",
        "import_processor",
        "_ingest",
        "_module_system",
        "_function_ingest",
    )

    def __init__(
        self,
        *,
        ingestor: IngestorProtocol,
        import_processor: ImportProcessor,
        ingest: JsTsIngest,
        module_system: JsTsModuleSystem,
        function_ingest: JsTsFunctionIngest,
    ) -> None:
        self.ingestor = ingestor
        self.import_processor = import_processor
        self._ingest = ingest
        self._module_system = module_system
        self._function_ingest = function_ingest

    def ingest_after_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        file_path: Path,
    ) -> None:
        self._module_system.ingest_missing_import_patterns(
            root_node, module_qn, language, queries
        )
        # Written AFTER parse_imports, which is what fills the set, and
        # unconditionally rather than only when non-empty: the node
        # already exists from the batch above, so the flush MERGEs and
        # `SET n += props`, which updates a property but never removes
        # one. Writing the empty list is therefore the only way a
        # re-parse can CLEAR specifiers whose target now exists
        # (issue #1714).
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.MODULE,
            {
                cs.KEY_QUALIFIED_NAME: module_qn,
                cs.KEY_UNRESOLVED_SPECIFIERS: sorted(
                    self.import_processor.js_ts.unresolved_specifiers.get(module_qn, ())
                ),
            },
        )

    def folded_overloads(
        self,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None,
    ) -> frozenset[int] | None:
        # Plain JavaScript declares no overload signatures.
        if language == cs.SupportedLanguage.JS:
            return None
        # The signatures are function captures, so the passes that
        # register them read the same nodes this folds.
        if combined_captures is None:
            found = get_function_captures(root_node, language, queries)
            combined_captures = found.captures if found else {}
        return folded_overload_signatures(
            combined_captures.get(cs.CAPTURE_FUNCTION, ())
        )

    def finalise(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        js_ts, module_system = self._ingest, self._module_system
        js_ts.ingest_object_literal_methods(root_node, module_qn, language, queries)
        module_system.ingest_commonjs_exports(root_node, module_qn, language, queries)
        module_system.ingest_es6_exports(root_node, module_qn, language, queries)
        js_ts.ingest_assignment_arrow_functions(root_node, module_qn, language, queries)
        js_ts.ingest_prototype_inheritance(root_node, module_qn, language, queries)
        # Named passes above have claimed their function nodes;
        # only genuinely anonymous spans (callbacks, IIFEs) still
        # need their held-back registration.
        self._function_ingest.flush_deferred_anonymous()
        # Direct module.exports functions finalise by SPAN now that
        # every node's minted qn is recorded.
        module_system.finalise_direct_module_exports(module_qn)
