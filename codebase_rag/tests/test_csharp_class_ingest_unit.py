"""`CSharpClassIngest`: a method's parameter-signature qn, what a C# type
records beside its node (declared namespace, generic arity, partial group,
location, members and method facts), and the base a deferred edge binds to
by `using`, by written arity or as an arity sibling (issues #2534, #2579)."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.csharp.class_ingest import (
    CSharpClassIngest,
    signatured_method_qn,
)
from codebase_rag.tests.collaborator_support import edges, index, nodes_of, parse
from codebase_rag.tests.conftest import get_nodes, get_relationships
from codebase_rag.types_defs import DeferredInherit
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "src/Widget.cs": (
        "namespace Acme.UI {\n"
        "  public partial class Widget { public int Size { get; set; } }\n"
        "}\n"
    ),
    "src/Widget.Designer.cs": (
        "namespace Acme.UI {\n  public partial class Widget { private Helper _h; }\n}\n"
    ),
    "src/Helper.cs": (
        "namespace Acme.UI {\n"
        "  public class Helper {\n"
        '    public virtual string Name() { return ""; }\n'
        "    public T Make<T>() { return default; }\n"
        "  }\n"
        "}\n"
    ),
    "src/Derived.cs": (
        "namespace Acme.UI {\n"
        "  public class Derived : Helper {\n"
        '    public override string Name() { return "d"; }\n'
        "    public new int GetHashCode() { return 0; }\n"
        "  }\n"
        "}\n"
    ),
    "src/Box.cs": (
        "namespace Acme.Data {\n"
        "  public class Box<T> { }\n"
        "  public class Box { }\n"
        "  public record Point(int X, Helper H);\n"
        "}\n"
    ),
    "src/PB.cs": "namespace Acme.Gen {\n  public class PB { }\n}\n",
    "src/PB.TResult.cs": "namespace Acme.Gen {\n  public class PB<TResult> : PB { }\n}\n",
    "src/Use.cs": (
        "using Acme.Data;\n"
        "namespace Acme.Use {\n  public class Holder : Box<int> { }\n}\n"
    ),
}

_BOX = "cs.src.Box.Acme.Data.Box"
_GENERIC_BOX = "cs.src.Box.Acme.Data.Box`1"
_PB = "cs.src.PB.Acme.Gen.PB"
_GENERIC_PB = "cs.src.PB.TResult.Acme.Gen.PB"
_WIDGET = "cs.src.Widget.Acme.UI.Widget"
_DESIGNER = "cs.src.Widget.Designer.Acme.UI.Widget"


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "cs", mock_ingestor, _FILES)


def _ingest(updater: GraphUpdater) -> CSharpClassIngest:
    ingest = updater.factory.definition_processor.class_ingest.csharp
    assert isinstance(ingest, CSharpClassIngest)
    return ingest


def _entry(
    child_qn: str, written_ref: str | None, module_qn: str = "cs.src.Use"
) -> DeferredInherit:
    return DeferredInherit(
        rel_type=cs.RelationshipType.INHERITS,
        child_qn=child_qn,
        parent_qn=f"{module_qn}.Base",
        module_qn=module_qn,
        base_index=0,
        language=cs.SupportedLanguage.CSHARP,
        written_ref=written_ref,
    )


def test_a_method_with_parameters_carries_its_signature() -> None:
    root = parse(
        cs.SupportedLanguage.CSHARP,
        "class C { void M(int a, string b) {} void Z() {} C(int x) {} }",
    )
    members = nodes_of(root, "method_declaration") + nodes_of(
        root, "constructor_declaration"
    )
    # A zero-arg member stays bare so an unsignatured call site matches it.
    assert [signatured_method_qn(m, "p.C") for m in members] == [
        "p.C.M(int, string)",
        None,
        "p.C.C(int)",
    ]


def test_a_type_records_its_namespace_and_written_arity(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    props = {
        str(n.properties[cs.KEY_QUALIFIED_NAME]): n.properties
        for n in get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
    }
    # The qn spells the CLR arity; the name is the one written.
    assert (
        props[_GENERIC_BOX][cs.KEY_NAME],
        props[_GENERIC_BOX][cs.KEY_NAMESPACE],
        props[_GENERIC_BOX][cs.KEY_GENERIC_ARITY],
    ) == ("Box", "Acme.Data", 1)
    assert cs.KEY_GENERIC_ARITY not in props[_BOX]
    facts = updater.factory.definition_processor.facts
    assert facts.csharp_namespaced_qns["Acme.Data.Box"] == {_BOX, _GENERIC_BOX}
    assert facts.csharp_class_namespaced[_BOX] == "Acme.Data.Box"
    # Only a generic records its arity and, since its qn cannot say it, the
    # module declaring it.
    assert facts.csharp_class_generic_arity == {_GENERIC_BOX: 1, _GENERIC_PB: 1}
    assert facts.csharp_class_owner_module[_GENERIC_PB] == "cs.src.PB.TResult"


def test_partial_parts_in_one_directory_share_one_group(updater: GraphUpdater) -> None:
    groups = updater.factory.definition_processor.facts.csharp_partial_groups
    # `Widget.Designer.cs` has a dotted stem; its directory is still `src`.
    assert groups[_WIDGET] is groups[_DESIGNER]
    assert sorted(groups[_WIDGET]) == [_WIDGET, _DESIGNER]
    assert _BOX not in groups
    assert _ingest(updater).namespaced_class("Acme.UI.Widget") in {_WIDGET, _DESIGNER}


def test_a_type_is_located_by_its_file_and_start_line(updater: GraphUpdater) -> None:
    locations = _ingest(updater).type_locations
    assert locations[("src/Box.cs", 2)] == _GENERIC_BOX
    assert locations[("src/Box.cs", 3)] == _BOX


def test_members_and_record_parameters_type_their_receivers(
    updater: GraphUpdater,
) -> None:
    field_types = updater.factory.definition_processor.facts.class_field_types
    assert field_types[_WIDGET] == {"Size": "int"}
    assert field_types[_DESIGNER] == {"_h": "Helper"}
    # A positional record parameter is a public property.
    assert field_types["cs.src.Box.Acme.Data.Point"] == {"X": "int", "H": "Helper"}


def test_only_an_override_modifier_puts_a_method_in_the_override_set(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = _ingest(updater)
    derived = "cs.src.Derived.Acme.UI.Derived"
    assert {f"{derived}.Name", f"{derived}.GetHashCode"} <= ingest.methods
    # `new` hides the base member; the override walk must not link it.
    assert ingest.override_methods == {f"{derived}.Name"}
    assert edges(mock_ingestor, cs.RelationshipType.OVERRIDES) == {
        (f"{derived}.Name", "cs.src.Helper.Acme.UI.Helper.Name")
    }


def test_a_methods_type_parameters_and_return_type_are_recorded(
    updater: GraphUpdater,
) -> None:
    facts = updater.factory.definition_processor.facts
    assert facts.csharp_generic_methods == {"cs.src.Helper.Acme.UI.Helper.Make"}
    # A property's type is its return type for a chained call.
    assert facts.csharp_method_return_types[f"{_WIDGET}.Size"] == ("int", 0)


def test_a_base_binds_through_its_using_at_its_written_arity(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    inherits = {
        (str(r.source), str(r.target)): r.properties
        for r in get_relationships(mock_ingestor, cs.RelationshipType.INHERITS)
    }
    # Reached by scope, so neither edge is marked heuristic.
    assert inherits[("cs.src.Use.Acme.Use.Holder", _GENERIC_BOX)] == {
        cs.KEY_BASE_INDEX: 0
    }
    assert inherits[(_GENERIC_PB, _PB)] == {cs.KEY_BASE_INDEX: 0}


def test_the_written_arity_moves_a_name_lookup_to_its_twin(
    updater: GraphUpdater,
) -> None:
    ingest = _ingest(updater)
    holder = "cs.src.Use.Acme.Use.Holder"
    assert ingest.base_by_arity(_entry(holder, "Box`1"), (_BOX, False)) == (
        _GENERIC_BOX,
        False,
    )
    # An external answer, or a base with no written form, is not C#'s to move.
    assert ingest.base_by_arity(_entry(holder, "Box`1"), ("Box", True)) == (
        "Box",
        True,
    )
    assert ingest.base_by_arity(_entry(holder, None), (_BOX, False)) == (_BOX, False)
    # The twin is the child itself: keep what the lookup found.
    assert ingest.base_by_arity(_entry(_GENERIC_BOX, "Box`1"), (_BOX, False)) == (
        _BOX,
        False,
    )


def test_a_base_naming_its_own_class_finds_the_arity_sibling(
    updater: GraphUpdater,
) -> None:
    ingest = _ingest(updater)
    # `class PB<TResult> : PB` in PB.TResult.cs names the type in PB.cs.
    assert ingest.arity_sibling(_entry(_GENERIC_PB, "PB", "cs.src.PB.TResult")) == _PB
    assert ingest.arity_sibling(_entry(_PB, "PB`1", "cs.src.PB")) == _GENERIC_PB


def test_a_namespaced_name_is_looked_up_by_its_declared_form(
    updater: GraphUpdater,
) -> None:
    ingest = _ingest(updater)
    assert ingest.namespaced_class("Acme.UI.Helper") == "cs.src.Helper.Acme.UI.Helper"
    # Two twins share the declared form: no single class.
    assert ingest.namespaced_class("Acme.Data.Box") is None
    assert ingest.namespaced_class("Acme.Nowhere") is None
