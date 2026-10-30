"""`CallerCalls`: the scan the call pass runs over one caller. It types the
caller's locals, writes the references its language finds in it whether or
not it makes a call, binds the call nodes it is handed (or its language's
call query finds), and puts the edge site back when it returns. A caller
whose local types cannot be built keeps its calls, resolved untyped."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from loguru import logger
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.call_scan import CallerCalls
from codebase_rag.parsers.type_inference import TypeInferenceEngine
from codebase_rag.tests.collaborator_support import first, index, nodes_of, parse
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.types_defs import LanguageQueries
from evals.recorded_graph import RecordingIngestor

pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

_PY = cs.SupportedLanguage.PYTHON
_RUNNER = "proj.m.runner"
_SOURCE = """\
class Box:
    def go(self):
        pass

def helper():
    pass

def other():
    pass

def runner():
    b = Box()
    b.go()
    cb = helper
    helper()
    other()
"""


@dataclass
class _Indexed:
    processor: CallProcessor
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    ingestor: RecordingIngestor

    @property
    def scan(self) -> CallerCalls:
        return self.processor.scan

    def ingest(self, call_nodes: list[Node] | None = None) -> None:
        self.scan.ingest(
            _runner(),
            _RUNNER,
            cs.NodeLabel.FUNCTION,
            "proj.m",
            _PY,
            self.queries,
            call_nodes=call_nodes,
        )

    def written(self, rel_type: str) -> dict[str, str]:
        # Each target the scan wrote from the caller, with its resolution.
        edges = get_relationships(self.ingestor, rel_type)
        assert {str(rel.source) for rel in edges} <= {_RUNNER}
        return {
            str(rel.target): str(rel.properties[cs.KEY_RESOLUTION]) for rel in edges
        }


@pytest.fixture
def indexed(temp_repo: Path, mock_ingestor: RecordingIngestor) -> _Indexed:
    updater = index(temp_repo / "proj", mock_ingestor, {"m.py": _SOURCE})
    # Only what the scan under test writes is under test.
    mock_ingestor.reset_mock()
    return _Indexed(updater.factory.call_processor, updater.queries, mock_ingestor)


@pytest.fixture
def errors() -> Generator[list[str], None, None]:
    logged: list[str] = []
    sink = logger.add(
        lambda message: logged.append(message.record["message"]), level="ERROR"
    )
    yield logged
    logger.remove(sink)


def _runner() -> Node:
    return next(
        node
        for node in nodes_of(parse(_PY, _SOURCE), cs.TS_PY_FUNCTION_DEFINITION)
        if (name := node.child_by_field_name(cs.FIELD_NAME)) is not None
        and name.text == b"runner"
    )


def _call(name: bytes) -> Node:
    return next(
        call
        for call in nodes_of(_runner(), cs.TS_PY_CALL)
        if (function := call.child_by_field_name(cs.FIELD_FUNCTION)) is not None
        and function.text == name
    )


def test_a_callers_queried_calls_are_bound_with_its_local_types(
    indexed: _Indexed,
) -> None:
    indexed.ingest()
    # `b` is typed `Box` by the scan, so `b.go()` binds exactly.
    assert indexed.written(cs.RelationshipType.CALLS) == {
        "proj.m.Box.go": cs.EdgeResolution.EXACT,
        "proj.m.helper": cs.EdgeResolution.EXACT,
        "proj.m.other": cs.EdgeResolution.EXACT,
    }
    assert set(indexed.written(cs.RelationshipType.INSTANTIATES)) == {"proj.m.Box"}
    assert set(indexed.written(cs.RelationshipType.REFERENCES)) == {"proj.m.helper"}


def test_the_call_nodes_handed_over_are_the_only_ones_bound(
    indexed: _Indexed,
) -> None:
    indexed.ingest(call_nodes=[_call(b"other")])
    assert set(indexed.written(cs.RelationshipType.CALLS)) == {"proj.m.other"}
    assert indexed.written(cs.RelationshipType.INSTANTIATES) == {}


def test_a_caller_with_no_calls_still_writes_its_references(
    indexed: _Indexed,
) -> None:
    indexed.ingest(call_nodes=[])
    # `cb = helper` keeps `helper` alive whether or not anything is called.
    assert set(indexed.written(cs.RelationshipType.REFERENCES)) == {"proj.m.helper"}
    assert indexed.written(cs.RelationshipType.CALLS) == {}


def test_the_scan_puts_the_edge_site_and_resolution_back(indexed: _Indexed) -> None:
    site = first(parse(_PY, "x\n"), cs.TS_PY_IDENTIFIER)
    indexed.processor.site_node = site
    indexed.processor.resolution = cs.EdgeResolution.OVERLOAD
    indexed.ingest()
    # Each call node's site and verdict were the scan's own; the caller
    # scanned next must not inherit the last of them.
    assert indexed.processor.site_node is site
    assert indexed.processor.resolution == cs.EdgeResolution.OVERLOAD
    assert {
        rel.properties[cs.KEY_LINE]
        for rel in get_relationships(indexed.ingestor, cs.RelationshipType.CALLS)
    } == {13, 15, 16}


def test_a_caller_whose_local_types_fail_keeps_its_calls_untyped(
    indexed: _Indexed, monkeypatch: pytest.MonkeyPatch, errors: list[str]
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("local types exploded")

    monkeypatch.setattr(TypeInferenceEngine, "build_local_variable_type_map", explode)
    indexed.ingest()

    # The failure costs the caller its local types, not its calls (#3173):
    # `b.go()` still binds, by name alone.
    assert indexed.written(cs.RelationshipType.CALLS) == {
        "proj.m.Box.go": cs.EdgeResolution.HEURISTIC,
        "proj.m.helper": cs.EdgeResolution.EXACT,
        "proj.m.other": cs.EdgeResolution.EXACT,
    }
    assert len(errors) == 1
    assert errors[0].startswith(f"Failed to infer local types in {_RUNNER} ")
    assert "local types exploded" in errors[0]
