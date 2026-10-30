"""Dart's share of the class pass: the type arguments an `extends` clause
passes and the type an extension is on, a class's field types and its
methods' return types, and which `@override` methods override only
external code.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships, and the deferred
INHERITS/IMPLEMENTS resolution) and hands Dart's part to the
`DartClassIngest` it builds."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ..class_ingest import parent_extraction as pe
from ..language_definitions import LanguageClassIngest
from . import utils as dart_utils
from .type_inference import DartTypeInferenceEngine

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol
    from ..definition_facts import DefinitionFacts


class DartClassHost(Protocol):
    """What Dart class ingestion calls on the class pass that builds it: the
    resolution a written base name goes through, which the type arguments of
    an `extends` clause are resolved by too."""

    def _resolve_to_qn(
        self,
        name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> str: ...


class DartClassIngest(LanguageClassIngest):
    """Records what a Dart class and its methods declare beside the shared
    class pass, and roots the `@override` methods no registered ancestor
    defines.

    Built and owned by `ClassIngest`. The registry, the definition facts and
    the annotated-override record are the pass's own, held by reference."""

    __slots__ = (
        "_host",
        "ingestor",
        "function_registry",
        "facts",
        "annotated_overrides",
    )

    def __init__(
        self,
        host: DartClassHost,
        *,
        ingestor: IngestorProtocol,
        function_registry: FunctionRegistryTrieProtocol,
        facts: DefinitionFacts,
        annotated_overrides: dict[str, list[tuple[str, str]]],
    ) -> None:
        self._host = host
        self.ingestor = ingestor
        self.function_registry = function_registry
        self.facts = facts
        # The `@override` methods of each class, recorded as it is ingested
        # and judged by flag_external_overrides once every class is.
        self.annotated_overrides = annotated_overrides

    def record_heritage(self, member_node: Node, class_qn: str, module_qn: str) -> None:
        if type_args := pe.extract_dart_extends_type_args(
            member_node,
            module_qn,
            partial(self._host._resolve_to_qn, language=cs.SupportedLanguage.DART),
        ):
            self.facts.dart_extends_type_args[class_qn] = type_args
        if on_type := dart_utils.extension_on_type(member_node):
            self.facts.dart_extension_on_types[class_qn] = on_type

    def record_field_types(self, class_node: Node, class_qn: str) -> None:
        # Record Dart field types (`Greeter buddy;`) so a field-typed
        # receiver (`buddy.greet()`, `this.buddy.hail()`) resolves
        # through the field's declared type.
        if field_types := DartTypeInferenceEngine().build_field_type_map(class_node):
            self.facts.class_field_types[class_qn] = field_types

    def record_method(
        self,
        class_node: Node,
        class_qn: str,
        module_qn: str | None,
        method_node: Node,
        ingested_qn: str,
    ) -> None:
        # Record Dart return types (a constructor "returns" its class)
        # so a local bound from a static factory or named constructor
        # (`var s = Greeter.create()`) types from the RECORDED return
        # instead of guessing the class from the call's base name.
        if dart_return := dart_utils.return_type_name(method_node):
            self.facts.method_return_types[ingested_qn] = dart_return
            if method_node.type in cs.DART_CONSTRUCTOR_SIGNATURE_TYPES:
                self.facts.dart_constructor_qns.add(ingested_qn)

    def flag_external_overrides(
        self,
        implements_map: dict[str, list[str]],
    ) -> None:
        # Every Dart class ultimately extends Object, so an @override whose
        # name NO registered ancestor defines can only target external code:
        # a framework base (direct or through any chain of first-party
        # classes), an implemented external interface, or Object itself
        # (toString/hashCode/operator==, invoked by interpolation and
        # collections). Those methods root the dead-code walk; a name a
        # registered ancestor defines resolves via OVERRIDES edges and must
        # stay an ordinary candidate. Runs from resolve_deferred_inherits
        # because ancestry is only known after every class is registered;
        # the partial row MERGEs into the node ingested during Pass 2.
        for child_qn, methods in self.annotated_overrides.items():
            for method_qn, method_name in methods:
                if self._registered_ancestor_defines(
                    child_qn, method_name, implements_map
                ):
                    continue
                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.METHOD,
                    {
                        cs.KEY_QUALIFIED_NAME: method_qn,
                        cs.KEY_OVERRIDES_EXTERNAL: True,
                    },
                )

    def _registered_ancestor_defines(
        self,
        class_qn: str,
        name: str,
        implements_map: dict[str, list[str]],
    ) -> bool:
        seen: set[str] = set()
        stack = list(self.facts.class_inheritance.get(class_qn, []))
        stack.extend(implements_map.get(class_qn, []))
        while stack:
            ancestor = stack.pop()
            if ancestor in seen:
                continue
            seen.add(ancestor)
            if self.function_registry.get(ancestor) is None:
                continue
            if (
                self.function_registry.get(f"{ancestor}{cs.SEPARATOR_DOT}{name}")
                is not None
            ):
                return True
            stack.extend(self.facts.class_inheritance.get(ancestor, []))
            stack.extend(implements_map.get(ancestor, []))
        return False
