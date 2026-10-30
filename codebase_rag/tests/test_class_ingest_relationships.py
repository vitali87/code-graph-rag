"""`class_ingest.relationships`: the bases a class records and the
INHERITS/IMPLEMENTS edges it writes at once or queues for the deferred pass
(the C++ queue with its namespace, a C# base with its written arity, a TS
declaration merged into an earlier one), and the labels and properties
those edges carry."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.class_ingest.relationships import (
    create_class_relationships,
    create_implements_relationship,
    create_inheritance_relationship,
    get_node_type_for_inheritance,
)
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.tests.collaborator_support import first, parse
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.types_defs import DeferredCppInherit, DeferredInherit, PropertyValue
from evals.recorded_graph import RecordingIngestor

_L = cs.SupportedLanguage
_CHILD = "proj.m.C"


class _Run:
    """One `create_class_relationships` call over a parsed snippet, with
    what it recorded."""

    def __init__(self, registry: FunctionRegistryTrie | None = None) -> None:
        self.ingestor = RecordingIngestor()
        self.registry = registry or FunctionRegistryTrie()
        self.inheritance: dict[str, list[str]] = {}
        self.implementers: dict[str, set[str]] = {}

    def __call__(
        self,
        language: cs.SupportedLanguage,
        source: str,
        node_type: str,
        *,
        deferred: list[DeferredInherit] | None = None,
        cpp_deferred: list[DeferredCppInherit] | None = None,
        exported: bool = False,
    ) -> None:
        create_class_relationships(
            first(parse(language, source), node_type),
            _CHILD,
            "proj.m",
            cs.NodeLabel.CLASS,
            exported,
            language,
            self.inheritance,
            self.ingestor,
            ImportProcessor(
                Path("/repo"),
                "proj",
                ingestor=self.ingestor,
                function_registry=self.registry,
            ),
            lambda name, module_qn: f"{module_qn}.{name}",
            self.registry,
            self.implementers,
            defer_cpp_inherits=cpp_deferred,
            defer_inherits=deferred,
        )

    def edges(self, rel_type: str) -> list[tuple[str, str, PropertyValue | None]]:
        return [
            (str(r.target), r.target_label, r.properties.get(cs.KEY_BASE_INDEX))
            for r in get_relationships(self.ingestor, rel_type)
        ]


def test_undeferred_bases_are_written_in_source_order() -> None:
    run = _Run()
    run(_L.PYTHON, "class C(A, mod.B):\n    pass\n", "class_definition")
    assert run.inheritance == {_CHILD: ["proj.m.A", "proj.m.mod.B"]}
    # base_index keeps the order an incremental run rehydrates the MRO from;
    # an unregistered base is labelled Class.
    assert run.edges(cs.RelationshipType.INHERITS) == [
        ("proj.m.A", cs.NodeLabel.CLASS, 0),
        ("proj.m.mod.B", cs.NodeLabel.CLASS, 1),
    ]


def test_deferred_bases_are_queued_instead_of_written() -> None:
    run = _Run()
    queue: list[DeferredInherit] = []
    run(_L.PYTHON, "class C(A, mod.B):\n    pass\n", "class_definition", deferred=queue)
    assert get_relationships(run.ingestor) == []
    assert [(e.parent_qn, e.base_index, e.child_label) for e in queue] == [
        ("proj.m.A", 0, cs.NodeLabel.CLASS),
        ("proj.m.mod.B", 1, cs.NodeLabel.CLASS),
    ]
    # The guesses are recorded now; the deferred pass replaces them in place.
    assert run.inheritance == {_CHILD: ["proj.m.A", "proj.m.mod.B"]}


def test_implemented_interfaces_are_written_and_their_implementers_recorded() -> None:
    run = _Run()
    run(_L.JAVA, "class C extends A implements I, J {}", "class_declaration")
    assert run.edges(cs.RelationshipType.IMPLEMENTS) == [
        ("proj.m.I", cs.NodeLabel.INTERFACE, None),
        ("proj.m.J", cs.NodeLabel.INTERFACE, None),
    ]
    assert run.implementers == {"proj.m.I": {_CHILD}, "proj.m.J": {_CHILD}}


def test_deferred_implements_still_record_their_implementers() -> None:
    # Dispatch to a sole implementer reads the pairs before the deferred
    # pass runs, so they cannot wait for it.
    run = _Run()
    queue: list[DeferredInherit] = []
    run(
        _L.JAVA,
        "class C extends A implements I {}",
        "class_declaration",
        deferred=queue,
    )
    assert [(e.rel_type, e.parent_qn) for e in queue] == [
        (cs.RelationshipType.INHERITS, "proj.m.A"),
        (cs.RelationshipType.IMPLEMENTS, "proj.m.I"),
    ]
    assert run.implementers == {"proj.m.I": {_CHILD}}


def test_a_cpp_base_waits_in_its_own_queue_with_its_namespace() -> None:
    run = _Run()
    queue: list[DeferredInherit] = []
    cpp_queue: list[DeferredCppInherit] = []
    run(
        _L.CPP,
        "namespace ns { class C : public A, private B {}; }",
        "class_specifier",
        deferred=queue,
        cpp_deferred=cpp_queue,
        exported=True,
    )
    assert queue == []
    assert [
        (e.base_name, e.guess_qn, e.namespace_path, e.base_index) for e in cpp_queue
    ] == [
        ("A", "proj.m.ns.A", "ns", 0),
        ("B", "proj.m.ns.B", "ns", 1),
    ]
    # An exported C++ class is the module's export; nothing else is written
    # until the bases resolve.
    assert [
        (str(r.source), r.rel_type, str(r.target))
        for r in get_relationships(run.ingestor)
    ] == [("proj.m", cs.RelationshipType.EXPORTS, _CHILD)]


def test_only_cpp_exports_its_exported_classes() -> None:
    run = _Run()
    run(_L.PYTHON, "class C:\n    pass\n", "class_definition", exported=True)
    assert get_relationships(run.ingestor, cs.RelationshipType.EXPORTS) == []


def test_a_csharp_base_carries_its_written_arity_to_the_deferred_pass() -> None:
    run = _Run()
    queue: list[DeferredInherit] = []
    run(
        _L.CSHARP,
        "namespace N { class C : Base<int>, IBar {} }",
        "class_declaration",
        deferred=queue,
    )
    assert [(e.rel_type, e.parent_qn, e.written_ref) for e in queue] == [
        (cs.RelationshipType.INHERITS, "proj.m.Base", "Base`1"),
        (cs.RelationshipType.IMPLEMENTS, "proj.m.IBar", "IBar"),
    ]


def test_a_merged_ts_declaration_continues_the_earlier_bases() -> None:
    # `interface C extends I0 {}` then `class C extends A {}`: one instance
    # type, so the class's base is the second of the list, not a new list.
    registry = FunctionRegistryTrie()
    registry.register_unique_qn(_CHILD, 1, kind=cs.NodeLabel.INTERFACE)
    registry[_CHILD] = cs.NodeLabel.INTERFACE
    registry.register_unique_qn(_CHILD, 2, kind=cs.NodeLabel.CLASS)
    registry[_CHILD] = cs.NodeLabel.CLASS
    run = _Run(registry)
    run.inheritance[_CHILD] = ["proj.m.I0"]
    queue: list[DeferredInherit] = []
    run(_L.TS, "class C extends A {}", "class_declaration", deferred=queue)
    assert run.inheritance == {_CHILD: ["proj.m.I0", "proj.m.A"]}
    assert [(e.parent_qn, e.base_index) for e in queue] == [("proj.m.A", 1)]


def test_a_redeclared_class_replaces_its_bases() -> None:
    run = _Run()
    run.inheritance[_CHILD] = ["proj.m.Old"]
    run(_L.PYTHON, "class C(New):\n    pass\n", "class_definition", deferred=[])
    assert run.inheritance == {_CHILD: ["proj.m.New"]}


def test_a_type_position_reads_the_type_twin_of_a_value() -> None:
    registry = FunctionRegistryTrie()
    registry.register_unique_qn("proj.m.Box", 1, kind=cs.NodeLabel.INTERFACE)
    registry["proj.m.Box"] = cs.NodeLabel.INTERFACE
    registry.register_unique_qn("proj.m.Box", 2, kind=cs.NodeLabel.FUNCTION)
    registry["proj.m.Box"] = cs.NodeLabel.FUNCTION
    assert (
        get_node_type_for_inheritance("proj.m.Box", registry) == cs.NodeLabel.FUNCTION
    )
    assert (
        get_node_type_for_inheritance("proj.m.Box", registry, type_position=True)
        == cs.NodeLabel.INTERFACE
    )
    assert get_node_type_for_inheritance("proj.m.Gone", registry) == cs.NodeLabel.CLASS


def test_an_interfaces_base_is_labelled_from_the_type_space() -> None:
    registry = FunctionRegistryTrie()
    registry.register_unique_qn("proj.m.Base", 1, kind=cs.NodeLabel.INTERFACE)
    registry["proj.m.Base"] = cs.NodeLabel.INTERFACE
    registry.register_unique_qn("proj.m.Base", 2, kind=cs.NodeLabel.CLASS)
    registry["proj.m.Base"] = cs.NodeLabel.CLASS
    ingestor = RecordingIngestor()
    create_inheritance_relationship(
        cs.NodeLabel.INTERFACE, "proj.m.I", "proj.m.Base", registry, ingestor
    )
    create_inheritance_relationship(
        cs.NodeLabel.CLASS,
        "proj.m.C",
        "proj.m.Base",
        registry,
        ingestor,
        base_index=2,
        resolution=cs.EdgeResolution.HEURISTIC,
    )
    interface_edge, class_edge = get_relationships(
        ingestor, cs.RelationshipType.INHERITS
    )
    assert (interface_edge.target_label, interface_edge.properties) == (
        cs.NodeLabel.INTERFACE,
        {cs.KEY_BASE_INDEX: 0},
    )
    assert (class_edge.target_label, class_edge.properties) == (
        cs.NodeLabel.CLASS,
        {cs.KEY_BASE_INDEX: 2, cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC},
    )


def test_an_implements_edge_carries_a_resolution_only_when_given_one() -> None:
    ingestor = RecordingIngestor()
    create_implements_relationship(cs.NodeLabel.CLASS, "proj.m.C", "proj.m.I", ingestor)
    create_implements_relationship(
        cs.NodeLabel.CLASS,
        "proj.m.D",
        "proj.m.Dart",
        ingestor,
        interface_label=cs.NodeLabel.CLASS,
        resolution=cs.EdgeResolution.HEURISTIC,
    )
    plain, labelled = get_relationships(ingestor, cs.RelationshipType.IMPLEMENTS)
    assert (plain.target_label, plain.properties) == (cs.NodeLabel.INTERFACE, {})
    assert (labelled.target_label, labelled.properties) == (
        cs.NodeLabel.CLASS,
        {cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC},
    )
