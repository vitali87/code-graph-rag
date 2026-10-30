"""`GoClassIngest`: where each Go type is declared (the index the go/types
facts join back through), what an interface embeds, what a struct's fields
are, and the interfaces whose overrides the Go interface link writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.go.class_ingest import GoClassIngest
from codebase_rag.tests.collaborator_support import index
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_A = (
    "package svc\n\n"
    'import "fmt"\n\n'
    "type Reader interface {\n\tfmt.Stringer\n\tRead() int\n}\n\n"
    "type Box struct {\n\tName string\n\tSize int\n}\n\n"
    "func (b *Box) Read() int { return b.Size }\n"
)


@pytest.fixture
def updater(
    temp_repo: Path, mock_ingestor: RecordingIngestor, monkeypatch: pytest.MonkeyPatch
) -> GraphUpdater:
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
    return index(temp_repo / "gp", mock_ingestor, {"svc/a.go": _A})


def _ingest(updater: GraphUpdater) -> GoClassIngest:
    ingest = updater.factory.definition_processor.class_ingest.go
    assert isinstance(ingest, GoClassIngest)
    return ingest


def test_each_type_is_indexed_by_where_it_is_declared(updater: GraphUpdater) -> None:
    assert _ingest(updater).type_locations == {
        ("svc/a.go", 5, 5): ("gp.svc.a.Reader", cs.NodeLabel.INTERFACE.value),
        ("svc/a.go", 10, 5): ("gp.svc.a.Box", cs.NodeLabel.CLASS.value),
    }


def test_a_structs_field_types_are_recorded(updater: GraphUpdater) -> None:
    facts = updater.factory.definition_processor.facts
    assert facts.class_field_types["gp.svc.a.Box"] == {"Name": "string", "Size": "int"}


def test_an_interface_node_records_what_it_embeds(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    embeds = [
        n.properties[cs.KEY_EMBEDS]
        for n in get_nodes(mock_ingestor, cs.NodeLabel.INTERFACE)
        if cs.KEY_EMBEDS in n.properties
    ]
    assert embeds == [["fmt.Stringer"]]


def test_go_overrides_are_linked_by_the_interface_pass(updater: GraphUpdater) -> None:
    ingest = _ingest(updater)
    assert set(ingest.overrides_linked_apart()) == set(ingest.interfaces)
    assert "gp.svc.a.Reader" in ingest.interfaces
    assert GoClassIngest.joins_read_back_implementers is False
