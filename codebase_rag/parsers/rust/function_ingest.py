"""Rust's share of the function pass: the items a function body, closure
or const initializer holds back until the names around them are claimed, a
free function's qualified name and return type, the REFERENCES a closure
gets from its scope, and the inline module that contains an item.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
Rust's part to the `RustFunctionIngest` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ..definition_facts import DefinitionFacts
from ..utils import function_span_key
from . import utils as rs_utils

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...services import IngestorProtocol
    from ...types_defs import FunctionSpanKey, LanguageQueries
    from ..function_identity import FunctionResolution


class RustFunctionHost(Protocol):
    """What Rust function ingestion calls on the function pass that builds
    it: the registration a held-back body-local item goes through once the
    file's other definitions have claimed their names."""

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> None: ...


class _DeferredBodyLocal(NamedTuple):
    """A Rust definition held back from the generic pass until later passes
    claim.

    A definition written inside a function body owns no name any caller can
    reach, so it must not take one from the method or module item that does
    (issue #1037). Registration happens at the per-file flush.
    """

    func_node: Node
    resolution: FunctionResolution
    module_qn: str
    lang_config: LanguageSpec
    lang_queries: LanguageQueries


class RustFunctionIngest:
    """Holds back Rust body-local items until their file's flush, and writes
    what a Rust function records beside the shared function pass.

    Built and owned by `FunctionIngest`. The ingestor and the definition
    facts are the pass's own, held by reference."""

    __slots__ = ("_host", "ingestor", "facts", "_deferred_body_local")

    def __init__(
        self, host: RustFunctionHost, ingestor: IngestorProtocol, facts: DefinitionFacts
    ) -> None:
        self._host = host
        self.ingestor = ingestor
        self.facts = facts
        self._deferred_body_local: list[_DeferredBodyLocal] = []

    def defers_body_local(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> bool:
        # A Rust item written inside a function body, a closure or a
        # const/static initializer is reachable by path from nowhere, yet it
        # registers in the qn space of the module or impl target that
        # encloses it. Registering it here would let it take the natural qn
        # of the method or module item of that name, which does own it and
        # whose callers then resolve to this one; hold it back until those
        # have claimed their names (issue #1037).
        if not rs_utils.is_body_local(func_node):
            return False
        self._deferred_body_local.append(
            _DeferredBodyLocal(
                func_node, resolution, module_qn, lang_config, lang_queries
            )
        )
        return True

    def flush_deferred_body_local(self) -> None:
        """Register the file's held-back Rust body-local items (issue #1037)."""
        deferred = self._deferred_body_local
        self._deferred_body_local = []
        for entry in deferred:
            self._host._register_function(
                entry.func_node,
                entry.resolution,
                entry.module_qn,
                cs.SupportedLanguage.RUST,
                entry.lang_config,
                entry.lang_queries,
            )
            # A call-bound local (`let s = make()`) types from this map, so the
            # return type has to be recorded here too. Under the qn the
            # registry HANDED OUT, which for a body-local item contesting a
            # module item of that name is the `@<line>` variant: recording
            # under the proposed qn would overwrite the module item's own
            # return type and mistype every caller of it.
            location = self.facts.function_locations.get(
                function_span_key(entry.module_qn, entry.func_node)
            )
            if location is not None and (
                return_type := rs_utils.extract_return_type_name(entry.func_node, None)
            ):
                self.facts.method_return_types[location.qualified_name] = return_type

    @staticmethod
    def qualified_name(func_node: Node, module_qn: str, func_name: str) -> str:
        path_parts = rs_utils.build_module_path(func_node)
        if path_parts:
            return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def record_return_type(self, func_node: Node, qualified_name: str) -> None:
        # A free Rust fn's return type (impl methods are recorded in class
        # ingest): a call-bound local (`let s = make()`) types from this map.
        # No impl target here, so a `Self` return stays None.
        if return_type := rs_utils.extract_return_type_name(func_node, None):
            self.facts.method_return_types[qualified_name] = return_type

    def reference_closure(
        self, func_node: Node, parent_label: str, parent_qn: str, closure_qn: str
    ) -> None:
        # A Rust closure is a value constructed at its definition site (a `.map`
        # arg, spawn body, `let` binding), so it is used wherever it is written.
        # Dead-code reachability walks CALLS/REFERENCES, not DEFINES, so mirror the
        # DEFINES with a REFERENCES from the enclosing scope, else every closure is
        # an orphan and reports dead. (JS/TS emit the analogous inline-callback ref.)
        if func_node.type != cs.TS_RS_CLOSURE_EXPRESSION:
            return
        self.ingestor.ensure_relationship_batch(
            (parent_label, cs.KEY_QUALIFIED_NAME, parent_qn),
            cs.RelationshipType.REFERENCES,
            (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, closure_qn),
        )

    @staticmethod
    def inline_module_parent(
        func_node: Node, func_qn: str
    ) -> tuple[str, str, FunctionSpanKey | None] | None:
        # A Rust item inside `mod inner` is contained by that inline module, not
        # the file module. Its enclosing module qn is the item's OWN qn minus the
        # item segment: re-deriving the mod path with build_module_path drops the
        # class scope an inline mod under a trait/impl body carries (foo.inner vs
        # foo.T.inner), so the DEFINES edge dangles off the inline Module node,
        # which keeps that scope (issue #1018). The item qn already carries it.
        if not rs_utils.build_module_path(func_node):
            return None
        nested = func_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return cs.NodeLabel.MODULE, nested, None
