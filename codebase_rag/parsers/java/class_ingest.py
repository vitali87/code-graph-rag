"""Java's share of the class pass: a method's parameter-signature qn, the
second location the javac facts key a method at, and the OVERRIDES edges an
anonymous class's methods get to its base type once every type is
registered.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships, and the override
walk) and hands Java's part to the `JavaClassIngest` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionLocation
from ..language_definitions import LanguageClassIngest
from . import utils as java_utils

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol
    from ..definition_facts import DefinitionFacts


def _anonymous_base_type(method_node: Node, class_node: Node) -> str | None:
    # If `method_node` sits inside an anonymous class body between it and
    # `class_node` (`new Base(){ ... m() ... }`), return the anon class's base type
    # name (the object_creation's `type` field, generic args stripped). None when the
    # method belongs directly to the enclosing class, not an anonymous subclass.
    current = method_node.parent
    while current is not None and current is not class_node:
        if current.type == cs.TS_CLASS_BODY:
            parent = current.parent
            if parent is not None and parent.type == cs.TS_OBJECT_CREATION_EXPRESSION:
                type_node = parent.child_by_field_name(cs.FIELD_TYPE)
                if type_node is not None and type_node.text is not None:
                    return type_node.text.decode(cs.ENCODING_UTF8).split(
                        cs.CHAR_ANGLE_OPEN, 1
                    )[0]
                return None
        current = current.parent
    return None


def signatured_method_qn(method_node: Node, class_qn: str) -> str | None:
    method_info = java_utils.extract_method_info(method_node)
    if method_name := method_info.get(cs.KEY_NAME):
        parameters = method_info.get(cs.KEY_PARAMETERS, [])
        param_sig = f"({','.join(parameters)})" if parameters else cs.EMPTY_PARENS
        return f"{class_qn}.{method_name}{param_sig}"
    return None


class JavaClassIngest(LanguageClassIngest):
    """Records what a Java method declares beside the shared class pass, and
    links anonymous-class overrides to their base type.

    Built and owned by `ClassIngest`. The registry, the definition facts and
    the anonymous-override record are the pass's own, held by reference: the
    function pass records a method-body anonymous override there too."""

    __slots__ = ("ingestor", "function_registry", "facts", "anon_overrides")

    def __init__(
        self,
        *,
        ingestor: IngestorProtocol,
        function_registry: FunctionRegistryTrieProtocol,
        facts: DefinitionFacts,
        anon_overrides: list[tuple[str, str, str, str]],
    ) -> None:
        self.ingestor = ingestor
        self.function_registry = function_registry
        self.facts = facts
        self.anon_overrides = anon_overrides

    def record_anonymous_override(
        self, method_node: Node, class_node: Node, ingested_qn: str, module_qn: str
    ) -> None:
        # A Java method declared inside an anonymous class body
        # (`new Base(){ @Override m(){} }`) is ingested here under the enclosing
        # class but really overrides the anon class's base type. Record it so a
        # deferred pass emits the OVERRIDES edge once the base is registered;
        # with override-reachability that keeps the dispatch-only override live.
        if base := _anonymous_base_type(method_node, class_node):
            method_name = ingested_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(
                cs.CHAR_PAREN_OPEN, 1
            )[0]
            self.anon_overrides.append((ingested_qn, method_name, base, module_qn))

    def register_name_alias(
        self, module_qn: str, method_node: Node, location: FunctionLocation
    ) -> None:
        # The javac frontend (issue #1181) keys a call target at the callee NAME
        # token, but function_span_key keys at the declaration start -- the
        # modifiers, so `    public String handle(` records col 4 while the tool
        # reports the col of `handle`. Register a SECOND location at the name so
        # the semantic join resolves, mirroring the Go alias.
        # setdefault, not assignment: two methods sharing a line could put this
        # alias on another method's span key, and the span record is the
        # authoritative one.
        name_node = method_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None:
            return
        self.facts.function_locations.setdefault(
            (module_qn, name_node.start_point[0] + 1, name_node.start_point[1]),
            location,
        )

    def resolve_anonymous_overrides(self) -> None:
        # Emit OVERRIDES edges for Java anonymous-class methods recorded at ingestion
        # (`new Base(){ @Override m(){} }`). The base type is resolved by UNIQUE
        # global simple-name match among type declarations (the base is usually in
        # another file; a full import resolve is unnecessary and this stays
        # revive-only; an ambiguous or unfound base is skipped). Each base method
        # directly on the base whose simple name matches gets an OVERRIDES edge from
        # the anon override, so override-reachability keeps the dispatch-only method
        # live when the base is reachable.
        type_decls = (cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE, cs.NodeLabel.ENUM)
        for anon_qn, method_name, base_type, _module_qn in self.anon_overrides:
            # A method-body anonymous override is registered as a FUNCTION (via the
            # function-ingest path), a field-initializer one as a METHOD. The graph
            # matches an edge endpoint by LABEL + qn, so emit the source with the qn's
            # ACTUAL registered label; a hard-coded Method label drops the edge for
            # the Function-labelled overrides (the eval matches by qn and would hide
            # this, but the production Cypher would not).
            anon_type = self.function_registry.get(anon_qn)
            if anon_type is None:
                continue
            anon_label = anon_type
            base_candidates = [
                qn
                for qn in self.function_registry.find_ending_with(base_type)
                if self.function_registry.get(qn) in type_decls
            ]
            if len(base_candidates) != 1:
                continue
            base_qn = base_candidates[0]
            base_prefix = f"{base_qn}{cs.SEPARATOR_DOT}"
            for qn, label in self.function_registry.find_with_prefix(base_qn):
                if (
                    label == cs.NodeLabel.METHOD
                    and qn.startswith(base_prefix)
                    and cs.SEPARATOR_DOT
                    not in qn[len(base_prefix) :].split(cs.CHAR_PAREN_OPEN, 1)[0]
                    and qn[len(base_prefix) :].split(cs.CHAR_PAREN_OPEN, 1)[0]
                    == method_name
                ):
                    self.ingestor.ensure_relationship_batch(
                        (anon_label, cs.KEY_QUALIFIED_NAME, anon_qn),
                        cs.RelationshipType.OVERRIDES,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, qn),
                    )
