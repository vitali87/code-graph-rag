"""A class body's methods: the Method node each function in it yields, and
where each one landed.

`ClassIngest` hands every class body it writes to the `ClassMethods` it
owns. The methods are the body's function captures, less an overload stub
that folds into its implementation and a member its language says repeats a
signature already taken; each is written through `method_ingest`, and the
qualified name and span it registered under are recorded for the call
pass, which attributes a method's calls by them instead of re-deriving
them. It calls back through `ClassMethodsHost` for the class collaborator of
a body's language."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import CppDefinitionSpan, FunctionLocation
from ..cpp.utils import record_cpp_definition_span
from ..definition_facts import DefinitionFacts
from ..language_definitions import LanguageClassIngest
from ..method_ingest import MethodIngestHost, ingest_method
from ..rust import utils as rs_utils
from ..utils import function_span_key
from .utils import body_method_nodes, find_child_by_type, skip_method

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries


class MethodScope(NamedTuple):
    # The per-class context every method of one class body is ingested under:
    # the node holding the body, the class's qn and label, and where it is.
    class_node: Node
    class_qn: str
    container_label: cs.NodeLabel
    language: cs.SupportedLanguage
    file_path: Path | None
    module_qn: str


class ClassMethodsHost(Protocol):
    """What a class body's method pass calls on the class pass that owns it:
    the class collaborator of the body's language."""

    def _for(self, language: cs.SupportedLanguage | None) -> LanguageClassIngest: ...


class ClassMethods:
    """Writes the methods of a class body and records where each landed.

    Built and owned by `ClassIngest`."""

    __slots__ = (
        "_host",
        "_method_host",
        "repo_path",
        "facts",
        "cpp_definition_spans",
        "folded_overloads",
    )

    def __init__(
        self,
        host: ClassMethodsHost,
        *,
        method_host: MethodIngestHost,
        repo_path: Path,
        facts: DefinitionFacts,
        cpp_definition_spans: dict[str, list[CppDefinitionSpan]],
        folded_overloads: dict[str, frozenset[int]],
    ) -> None:
        self._host = host
        # What a class's methods are written through, and where a
        # definition's docstring is read from.
        self._method_host = method_host
        self.repo_path = repo_path
        self.facts = facts
        self.cpp_definition_spans = cpp_definition_spans
        self.folded_overloads = folded_overloads

    def ingest(
        self,
        scope: MethodScope,
        lang_queries: LanguageQueries,
        sorted_func_nodes: list[Node] | None,
        func_node_starts: list[int] | None,
    ) -> None:
        """Write the methods of the body `scope.class_node` holds."""
        class_node, class_qn = scope.class_node, scope.class_qn
        body_node = class_node.child_by_field_name(cs.FIELD_BODY)
        if body_node is None and class_node.type == cs.TS_DART_MIXIN_DECLARATION:
            # mixin_declaration is the one Dart type declaration whose
            # class_body is a positional child, not a `body` field; without
            # this fallback a mixin's members silently vanish.
            body_node = find_child_by_type(class_node, cs.TS_DART_CLASS_BODY)
        if not body_node:
            return

        lang_config = lang_queries[cs.QUERY_CONFIG]
        language_ingest = self._host._for(scope.language)
        member_aliases = language_ingest.member_aliases(body_node)

        method_nodes = body_method_nodes(
            body_node, lang_queries, sorted_func_nodes, func_node_starts
        )
        if method_nodes is None:
            return
        method_nodes = self._without_overload_stubs(method_nodes, scope.module_qn)

        external_override_names = language_ingest.external_override_names(class_qn)
        annotated_override_sink = language_ingest.annotated_override_sink()
        seen_signatures: set[str] = set()

        for method_node in method_nodes:
            if skip_method(method_node, class_node, body_node, lang_config):
                continue
            if language_ingest.repeats_member_signature(method_node, seen_signatures):
                continue

            ingested_qn = ingest_method(
                self._method_host,
                method_node,
                class_qn,
                scope.container_label,
                scope.language,
                lang_queries=lang_queries,
                method_qualified_name=language_ingest.method_qualified_name(
                    method_node, class_qn
                ),
                file_path=scope.file_path,
                module_qn=scope.module_qn,
                external_override_names=external_override_names,
                annotated_override_sink=annotated_override_sink,
                member_aliases=member_aliases.get(method_node.id, ()),
            )
            if ingested_qn is not None:
                self._record_ingested_method(scope, method_node, ingested_qn)
            language_ingest.record_method_return_type(method_node, class_qn)

    def _without_overload_stubs(
        self, method_nodes: list[Node], module_qn: str
    ) -> list[Node]:
        # The implementation after an `@overload` stub owns the method's
        # name; the stub is no method of its own (issue #2590), nor is a TS
        # namespace member's overload signature (issue #2457).
        stubs = self.folded_overloads.get(module_qn)
        if not stubs:
            return method_nodes
        return [node for node in method_nodes if node.start_byte not in stubs]

    def _record_ingested_method(
        self, scope: MethodScope, method_node: Node, ingested_qn: str
    ) -> None:
        language = scope.language
        module_qn = scope.module_qn
        self._host._for(language).record_method(
            scope.class_node, scope.class_qn, module_qn, method_node, ingested_qn
        )
        # Rust trait bodies reach here rather than the impl path above;
        # a trait is no module either (issue #1086).
        rs_utils.record_effective_module(
            self.facts.rust_function_modules,
            ingested_qn,
            method_node,
            module_qn,
            language,
        )
        record_cpp_definition_span(
            self.cpp_definition_spans,
            language,
            scope.file_path,
            self.repo_path,
            method_node,
            cs.NodeLabel.METHOD.value,
            ingested_qn,
        )
        # Record where this method landed so Pass-3 call attribution
        # reuses the registered qn/label instead of re-deriving them.
        # The walks diverge on preprocessor-distorted C++ class bodies
        # and on TS declaration merging, where the member registers
        # under the namespace's duplicate-suffixed qn (issue #652).
        self._record_method_location(scope, method_node, ingested_qn)

    def _record_method_location(
        self, scope: MethodScope, method_node: Node, ingested_qn: str
    ) -> None:
        module_qn = scope.module_qn
        span = function_span_key(module_qn, method_node)
        location = FunctionLocation(
            label=cs.NodeLabel.METHOD.value,
            qualified_name=ingested_qn,
            container_qn=scope.class_qn,
        )
        language_ingest = self._host._for(scope.language)
        if (
            not language_ingest.keeps_claimed_method_spans
            or span not in self.facts.function_locations
        ):
            self.facts.function_locations[span] = location
        language_ingest.register_name_alias(module_qn, method_node, location)
