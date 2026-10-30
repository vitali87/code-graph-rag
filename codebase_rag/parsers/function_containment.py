"""Where a function is defined: the parent its DEFINES edge comes from, and
the edges held until every parent has registered.

`FunctionIngest` hands every containment it writes to the
`FunctionContainment` it owns, and so does the class pass, whose methods
defer their containment to the same queue. A module parent is written
straight away, since module nodes always exist; any other parent may be
registered by a later pass or be a phantom the database would drop, so its
edge waits in `deferred_parent_links` for `resolve_deferred_parent_links`,
which binds each to the parent that did register. It calls back through
`ContainmentHost` for the collaborator of a function's language and for the
identity of an enclosing function."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import LanguageSpec
from ..types_defs import (
    DeferredParentLink,
    FunctionLocation,
    FunctionSpanKey,
)
from .function_identity import FunctionResolution
from .language_definitions import LanguageFunctionIngest
from .utils import function_span_key

if TYPE_CHECKING:
    from .definition_context import DefinitionContext


class ContainmentHost(Protocol):
    """What a function's containment calls on the function pass that owns
    it: the function collaborator of a language, whether a function node is
    a method, and the identity an enclosing function registered under."""

    def _for(
        self, language: cs.SupportedLanguage | str | None
    ) -> LanguageFunctionIngest: ...

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool: ...

    def _resolve_function_identity(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> FunctionResolution | None: ...


class FunctionContainment:
    """Finds a function's parent and writes, or holds, its DEFINES edge.

    Built and owned by `FunctionIngest`."""

    __slots__ = (
        "_host",
        "ingestor",
        "function_registry",
        "module_qn_to_file_path",
        "facts",
        "_deferred_parent_links",
    )

    def __init__(
        self,
        host: ContainmentHost,
        context: DefinitionContext,
        deferred_parent_links: list[DeferredParentLink],
    ) -> None:
        self._host = host
        self.ingestor = context.ingestor
        self.function_registry = context.function_registry
        self.module_qn_to_file_path = context.module_qn_to_file_path
        self.facts = context.facts
        # Shared with the class pass, whose methods defer their containment
        # here too; emptied in place by resolve_deferred_parent_links.
        self._deferred_parent_links = deferred_parent_links

    def emit_or_defer_defines(
        self,
        parent_label: str,
        parent_qn: str,
        child_label: str,
        child_qn: str,
        module_qn: str,
        fallback_label: str | None = None,
        fallback_qn: str | None = None,
        parent_span: FunctionSpanKey | None = None,
    ) -> None:
        # Module nodes always exist, so module-parented edges emit directly. Any
        # other parent may be registered by a later pass (methods land after
        # functions) or may be a phantom recomputed qn the database would drop; both
        # resolve in resolve_deferred_parent_links, where the optional fallback (a
        # nested child's lexical enclosing function) beats the module anchor.
        if parent_label == cs.NodeLabel.MODULE:
            self.ingestor.ensure_relationship_batch(
                (parent_label, cs.KEY_QUALIFIED_NAME, parent_qn),
                cs.RelationshipType.DEFINES,
                (child_label, cs.KEY_QUALIFIED_NAME, child_qn),
            )
            return
        self._deferred_parent_links.append(
            DeferredParentLink(
                parent_label_guess=parent_label,
                parent_qn=parent_qn,
                child_label=child_label,
                child_qn=child_qn,
                module_qn=module_qn,
                fallback_label=fallback_label,
                fallback_qn=fallback_qn,
                parent_span=parent_span,
            )
        )

    def _claimed_qn_for_anonymous_guess(
        self, module_qn: str, parent_qn: str
    ) -> tuple[str, str] | None:
        # An `anonymous_row_col` guess names the SPAN it stood for; if a named pass
        # claimed that span, the claim is the real parent.
        tail = parent_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if not tail.startswith(cs.PREFIX_ANONYMOUS):
            return None
        row_col = tail[len(cs.PREFIX_ANONYMOUS) :].split(cs.CHAR_UNDERSCORE)
        if len(row_col) != 2 or not all(part.isdigit() for part in row_col):
            return None
        loc = self.facts.function_locations.get(
            (module_qn, int(row_col[0]) + 1, int(row_col[1]))
        )
        if loc is None or loc.qualified_name not in self.function_registry:
            return None
        return loc.label, loc.qualified_name

    def _recorded_parent_at_span(
        self, entry: DeferredParentLink
    ) -> FunctionLocation | None:
        # The span pins the parent NODE, so the location recorded for it in the
        # class pass carries the exact registered identity, including a C# overload's
        # signature suffix that the guessed qn cannot (and that a parameterless
        # sibling overload exactly shadows).
        if entry.parent_span is None:
            return None
        recorded = self.facts.function_locations.get(entry.parent_span)
        if recorded is None or recorded.qualified_name not in self.function_registry:
            return None
        return recorded

    def resolve_deferred_parent_links(self) -> int:
        """Emit deferred non-module DEFINES once every pass has registered.

        A registered parent gets the edge under its real label; an
        unregistered parent qn is a phantom, so the child anchors to its
        module rather than losing the edge at the database MERGE.
        """
        deferred = self._deferred_parent_links
        if not deferred:
            return 0
        emitted = 0
        for entry in deferred:
            parent_spec, rel_type = self._resolved_parent(entry)
            self.ingestor.ensure_relationship_batch(
                parent_spec,
                rel_type,
                (entry.child_label, cs.KEY_QUALIFIED_NAME, entry.child_qn),
            )
            emitted += 1
        self._deferred_parent_links.clear()
        return emitted

    def _resolved_parent(
        self, entry: DeferredParentLink
    ) -> tuple[tuple[str, str, str], str]:
        # The parent a held DEFINES edge binds to, and the edge's type.
        if recorded := self._recorded_parent_at_span(entry):
            return (
                cs.NodeLabel(recorded.label),
                cs.KEY_QUALIFIED_NAME,
                recorded.qualified_name,
            ), entry.rel_type
        if registered := self.function_registry.get(entry.parent_qn):
            return (registered, cs.KEY_QUALIFIED_NAME, entry.parent_qn), entry.rel_type
        if (
            entry.fallback_qn is not None
            and entry.fallback_label is not None
            and self.function_registry.get(entry.fallback_qn) is not None
        ):
            # The parent guess never registered but the child's lexical
            # enclosing function did (a prototype assignment on a parameter
            # inside a function body); the lexical parent is the true
            # containment, not the module.
            return (
                cs.NodeLabel(entry.fallback_label),
                cs.KEY_QUALIFIED_NAME,
                entry.fallback_qn,
            ), cs.RelationshipType.DEFINES.value
        if claimed := self._claimed_qn_for_anonymous_guess(
            entry.module_qn, entry.parent_qn
        ):
            # The parent guess was an anonymous placeholder for a span a NAMED
            # pass claimed after the child registered
            # (`exports.receiver = function () { function helper() }`): the
            # placeholder embeds its (row, col), so the claim record recovers
            # the registered enclosing node.
            claimed_label, claimed_qn = claimed
            return (
                cs.NodeLabel(claimed_label),
                cs.KEY_QUALIFIED_NAME,
                claimed_qn,
            ), cs.RelationshipType.DEFINES.value
        # A method whose container never registered (impl on a primitive,
        # macro-corrupted class) anchors to its module with DEFINES;
        # DEFINES_METHOD from a Module is not a documented shape.
        return (
            cs.NodeLabel.MODULE,
            cs.KEY_QUALIFIED_NAME,
            entry.module_qn,
        ), cs.RelationshipType.DEFINES.value

    def _enclosing_function_parent(
        self,
        current: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None,
        file_path: Path | None,
    ) -> tuple[str, str, FunctionSpanKey | None] | None:
        # The parent a function nested in `current` (an enclosing function)
        # binds to; None when no distinct parent qn can be derived.
        language_ingest = self._host._for(language)
        parent_label = (
            cs.NodeLabel.METHOD
            if self._host._is_method(current, lang_config)
            else cs.NodeLabel.FUNCTION
        )
        # Bind to the enclosing function's OWN qn, recomputed from its node.
        # A function nested in an anonymous callback otherwise loses that
        # callback: anonymous scopes contribute no segment to the child qn,
        # so trimming would skip the callback and hoist the child to the
        # nearest named ancestor.
        if enclosing := language_ingest.enclosing_parent(current, func_qn, module_qn):
            return enclosing
        resolution = (
            self._host._resolve_function_identity(
                current, module_qn, language, lang_config, file_path
            )
            if language is not None
            else None
        )
        parent_qn = (
            resolution.qualified_name
            if resolution
            else func_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        )
        if not parent_qn or parent_qn == func_qn:
            return None
        span = (
            function_span_key(module_qn, current)
            if language_ingest.pins_parent_span
            else None
        )
        return parent_label, parent_qn, span

    def determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str, FunctionSpanKey | None]:
        current = func_node.parent
        if not isinstance(current, Node):
            return cs.NodeLabel.MODULE, module_qn, None

        file_path = self.module_qn_to_file_path.get(module_qn)
        transparent = self._host._for(language).transparent_parent_types
        while current and (
            current.type in transparent
            or current.type not in lang_config.module_node_types
        ):
            if (
                current.type not in transparent
                and current.type in lang_config.function_node_types
            ):
                parent = self._enclosing_function_parent(
                    current, func_qn, module_qn, lang_config, language, file_path
                )
                if parent is None:
                    break
                return parent

            current = current.parent

        if inline_parent := self._host._for(language).inline_module_parent(
            func_node, func_qn
        ):
            return inline_parent

        return cs.NodeLabel.MODULE, module_qn, None
