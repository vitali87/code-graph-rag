"""`GoInterfaceIngest` (issue #2483): what an interface's method specs record
at class ingest, and what the link writes once every type is registered:
INHERITS for a first-party embed, a heuristic IMPLEMENTS for each type whose
methods cover a method set, an OVERRIDES per method satisfied, the
interfaces whose implementers cannot be told, and the swap of one source's
pairs that keeps a reused updater from holding a stale implementer."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.go import interfaces as go_ifaces
from codebase_rag.parsers.go.interface_ingest import GoInterfaceIngest
from codebase_rag.tests.collaborator_support import edges, first, index, parse
from codebase_rag.tests.conftest import get_nodes, get_relationships
from codebase_rag.types_defs import FunctionLocation
from evals.recorded_graph import RecordingIngestor

# Every test indexes its own small module, and each index would otherwise
# ask the toolchains for their versions again.
pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

_SHAPES = """package shapes

type Shape interface {
\tArea() float64
}

type Solid interface {
\tShape
\terror
\tVolume() float64
}

type Remote interface {
\tio.Reader
}

type Square struct{ S float64 }

func (s Square) Area() float64 { return s.S * s.S }

type Cube struct{ S float64 }

func (c Cube) Area() float64 { return 6 * c.S * c.S }

func (c Cube) Volume() float64 { return c.S * c.S * c.S }

func (c Cube) Error() string { return "" }

type NotAShape struct{}

func (n NotAShape) Perimeter() float64 { return 0 }
"""

_MODULE = "gp.shapes.shapes"
_SHAPE = f"{_MODULE}.Shape"
_SOLID = f"{_MODULE}.Solid"
_REMOTE = f"{_MODULE}.Remote"
_SQUARE = f"{_MODULE}.Square"
_CUBE = f"{_MODULE}.Cube"


@pytest.fixture
def updater(
    temp_repo: Path, mock_ingestor: RecordingIngestor, monkeypatch: pytest.MonkeyPatch
) -> GraphUpdater:
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    return index(
        temp_repo / "gp",
        mock_ingestor,
        {"go.mod": "module gp\n\ngo 1.22\n", "shapes/shapes.go": _SHAPES},
    )


def _ingest(updater: GraphUpdater) -> GoInterfaceIngest:
    ingest = updater.factory.definition_processor.class_ingest.go.interface_ingest
    assert isinstance(ingest, GoInterfaceIngest)
    return ingest


def test_an_interface_records_its_embeds_and_method_specs(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = _ingest(updater)
    assert ingest.go_interfaces[_SOLID] == go_ifaces.GoInterfaceDecl(
        ("Shape", "error"), "shapes:shapes", "shapes/shapes.go"
    )
    assert ingest.go_methods[f"{_SHAPE}.Area"] == go_ifaces.GoMethodSpec(
        go_ifaces.GoSignature((), ("float64",)), "shapes:shapes", "shapes/shapes.go"
    )
    methods = {
        str(node.properties[cs.KEY_QUALIFIED_NAME])
        for node in get_nodes(mock_ingestor, cs.NodeLabel.METHOD)
    }
    assert {f"{_SHAPE}.Area", f"{_SOLID}.Volume"} <= methods


def test_an_interface_method_is_located_at_its_name_token(
    updater: GraphUpdater,
) -> None:
    # The go/types frontend names a call through an interface by the
    # method's NAME, here line 4, column 1 after the tab.
    assert _ingest(updater).facts.function_locations[
        (_MODULE, 4, 1)
    ] == FunctionLocation(
        label=cs.NodeLabel.METHOD.value,
        qualified_name=f"{_SHAPE}.Area",
        container_qn=_SHAPE,
    )


def test_a_type_spec_that_is_no_interface_records_nothing(
    updater: GraphUpdater,
) -> None:
    ingest = _ingest(updater)
    interfaces, methods = dict(ingest.go_interfaces), dict(ingest.go_methods)
    struct = first(
        parse(cs.SupportedLanguage.GO, "package p\n\ntype Box struct{ N int }\n"),
        cs.TS_GO_TYPE_SPEC,
    )
    ingest.ingest_interface_methods(struct, f"{_MODULE}.Box", _MODULE, None)
    assert ingest.go_interfaces == interfaces
    assert ingest.go_methods == methods


def test_a_type_covering_a_method_set_implements_it_by_guess(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    implements = get_relationships(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    # Cube covers Solid through its embedded Shape and predeclared error.
    assert {(str(r.source), str(r.target)) for r in implements} == {
        (_SQUARE, _SHAPE),
        (_CUBE, _SHAPE),
        (_CUBE, _SOLID),
    }
    assert {r.properties[cs.KEY_RESOLUTION] for r in implements} == {
        cs.EdgeResolution.HEURISTIC
    }
    ingest = _ingest(updater)
    assert ingest.go_guessed_pairs == {
        (_SHAPE, _SQUARE),
        (_SHAPE, _CUBE),
        (_SOLID, _CUBE),
    }
    assert ingest.facts.interface_implementers == {
        _SHAPE: {_SQUARE, _CUBE},
        _SOLID: {_CUBE},
    }


def test_only_a_first_party_embed_is_inherited(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert edges(mock_ingestor, cs.RelationshipType.INHERITS) == {(_SOLID, _SHAPE)}
    assert _ingest(updater).facts.class_inheritance[_SOLID] == [_SHAPE]


def test_each_implementing_method_overrides_the_method_it_satisfies(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert edges(mock_ingestor, cs.RelationshipType.OVERRIDES) == {
        (f"{_SQUARE}.Area", f"{_SHAPE}.Area"),
        (f"{_CUBE}.Area", f"{_SHAPE}.Area"),
        (f"{_CUBE}.Volume", f"{_SOLID}.Volume"),
    }


def test_an_interface_embedding_an_unresolved_name_has_unknown_implementers(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    marks = {
        str(node.properties[cs.KEY_QUALIFIED_NAME]): node.properties[
            cs.KEY_IMPLEMENTERS_UNKNOWN
        ]
        for node in get_nodes(mock_ingestor, cs.NodeLabel.INTERFACE)
        if cs.KEY_IMPLEMENTERS_UNKNOWN in node.properties
    }
    # `io` is not imported, so Remote's method set is unknown; every
    # interface is written, so a sync clears a mark that no longer holds.
    assert marks == {
        _REMOTE: cs.GoImplementersUnknown.EMBEDS.value,
        _SHAPE: "",
        _SOLID: "",
    }


def test_a_pair_the_compiler_compared_holds_only_if_it_proved_it(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = _ingest(updater)
    # As the go/types join would have left it: one pair proven.
    ingest.go_proven_pairs.add((_SHAPE, _SQUARE))
    written = len(get_relationships(mock_ingestor, cs.RelationshipType.IMPLEMENTS))

    linked = ingest.link_go_interfaces(
        frozenset({"."}),
        frozenset({"shapes/shapes.go"}),
        frozenset({_SHAPE, _SOLID, _SQUARE, _CUBE}),
    )

    # Every pair is the compiler's to decide, so none is guessed again, and
    # the guesses the first link made are taken back.
    assert linked == 0
    assert (
        len(get_relationships(mock_ingestor, cs.RelationshipType.IMPLEMENTS)) == written
    )
    assert ingest.go_guessed_pairs == set()
    assert ingest.facts.interface_implementers == {_SHAPE: {_SQUARE}, _SOLID: set()}


def test_replacing_one_sources_pairs_keeps_the_other_sources(
    updater: GraphUpdater,
) -> None:
    ingest = _ingest(updater)
    ingest.facts.interface_implementers.clear()
    ingest.facts.interface_implementers["I"] = {"A", "B", "X"}
    previous = {("I", "A"), ("I", "B")}

    ingest.replace_go_implementers(
        previous, [("I", "C"), ("J", "A")], keep={("I", "B")}
    )

    # A is gone; B is the other source's too; X was never this source's.
    assert ingest.facts.interface_implementers == {"I": {"B", "X", "C"}, "J": {"A"}}
    assert previous == {("I", "C"), ("J", "A")}
