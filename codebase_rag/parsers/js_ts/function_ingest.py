"""JS/TS's share of the function pass: the anonymous function expressions
it holds back until the named JS/TS passes have claimed their spans, the
name scope a function's name is visible in, and the registered identity a
nested function's parent is read from.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
JS/TS's part to the `JsTsFunctionIngest` it builds; the named JS/TS passes
are `JsTsIngest` and `JsTsModuleSystem`."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol, PropertyDict
from ..definition_facts import DefinitionFacts
from ..language_definitions import LanguageFunctionIngest
from ..utils import function_span_key
from . import utils as js_ts_utils

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import FunctionSpanKey, LanguageQueries
    from ..function_identity import FunctionResolution


class JsTsFunctionHost(Protocol):
    """What JS/TS function ingestion calls on the function pass that builds
    it: whether a named pass has claimed a function's span, and the
    registration an unclaimed anonymous function goes through after all."""

    def _function_span_claimed(self, module_qn: str, func_node: Node) -> bool: ...

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> None: ...


class _DeferredAnonymous(NamedTuple):
    """A function held back from the generic pass until later passes claim.

    An unnamed JS/TS function expression may be one a named pass registers
    under its real name, and registering `anonymous_row_col` eagerly minted a
    second node for every one of them (521 locations on thrift's JS corpora).
    Registration happens at the per-file flush.
    """

    func_node: Node
    resolution: FunctionResolution
    module_qn: str
    language: cs.SupportedLanguage
    lang_config: LanguageSpec
    lang_queries: LanguageQueries


class JsTsFunctionIngest(LanguageFunctionIngest):
    """Holds back anonymous JS/TS function expressions until their file's
    flush, and marks what a JS/TS function's name is visible to.

    Built and owned by `FunctionIngest`. The registry and the definition
    facts are the pass's own, held by reference."""

    __slots__ = ("_host", "function_registry", "facts", "_deferred_anonymous")

    def __init__(
        self,
        host: JsTsFunctionHost,
        function_registry: FunctionRegistryTrieProtocol,
        facts: DefinitionFacts,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.facts = facts
        # Unnamed JS/TS function expressions held back until the named
        # JS passes have claimed their spans (one node per source function).
        self._deferred_anonymous: list[_DeferredAnonymous] = []

    def defers_anonymous(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> bool:
        # A nameless JS/TS function expression may be one a NAMED pass
        # (object literal, export, assignment, prototype) registers under its
        # real name; those passes run after this one, so hold the anonymous
        # registration back and flush only unclaimed spans (each eager
        # registration was a duplicate node).
        if not resolution.is_anonymous:
            return False
        self._deferred_anonymous.append(
            _DeferredAnonymous(
                func_node,
                resolution,
                module_qn,
                language,
                lang_config,
                lang_queries,
            )
        )
        return True

    def flush_deferred_anonymous(self) -> None:
        deferred = self._deferred_anonymous
        self._deferred_anonymous = []
        for entry in deferred:
            if self._host._function_span_claimed(entry.module_qn, entry.func_node):
                continue
            self._host._register_function(
                entry.func_node,
                entry.resolution,
                entry.module_qn,
                entry.language,
                entry.lang_config,
                entry.lang_queries,
            )

    def mark_name_scope(
        self, func_node: Node, qualified_name: str, func_props: PropertyDict
    ) -> None:
        if js_ts_utils.is_object_literal_method(func_node):
            # `{delay () {...}}` registers here, under its key, before the
            # object-literal pass would; only its object reaches it (issue
            # #2435), and the persisted mark keeps that across incremental runs.
            func_props[cs.KEY_IS_OBJECT_MEMBER] = True
            self.function_registry.mark_object_member(qualified_name)
        # A JS/TS function expression whose name only its own body can call:
        # bare-name resolution skips it everywhere else (issue #2402), and the
        # persisted property lets an incremental run's rehydrated registry
        # skip it too (the is_macro pattern).
        if js_ts_utils.name_is_body_scoped(func_node):
            func_props[cs.KEY_IS_BODY_SCOPED_NAME] = True
            self.function_registry.mark_body_scoped_name(qualified_name)

    def recorded_parent(
        self, current: Node, func_qn: str, module_qn: str
    ) -> tuple[str, str, FunctionSpanKey | None] | None:
        # Reuse the enclosing function's REGISTERED identity when its span is
        # claimed: structural re-derivation produces the pre-claim qn (an
        # anonymous name whose node no longer exists for
        # `exports.f = function`, or the FIRST `t` for the second same-name
        # fn expr registered as `t@line`), hoisting the child to the module
        # or the wrong function.
        recorded = self.facts.function_locations.get(
            function_span_key(module_qn, current)
        )
        if (
            recorded is not None
            and recorded.qualified_name != func_qn
            and recorded.qualified_name in self.function_registry
        ):
            return (
                cs.NodeLabel(recorded.label),
                recorded.qualified_name,
                None,
            )
        return None
