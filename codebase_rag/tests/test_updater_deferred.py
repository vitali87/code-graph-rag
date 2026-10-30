"""`DeferredResolution`: the definition-level stages that follow Pass 2.

Inheritance, type annotations and the module map the IMPORTS flush verifies
against all resolve once every file of the run is parsed, and on an
incremental run once the registry of the files it did not re-parse is read
back from the graph (issue #1524). These tests run Pass 2 by hand over real
files, construct the stages directly from that updater's collaborators, and
read what they emitted.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import IngestorProtocol
from codebase_rag.services.graph_service import GraphConnection, GraphIngestor
from codebase_rag.tests.conftest import recorded_triples
from codebase_rag.types_defs import ResultRow
from codebase_rag.updater.deferred import DeferredResolution
from evals.recorded_graph import RecordingIngestor

_BASE = "class Base:\n    pass\n\n\nclass Widget:\n    pass\n"
_CHILD = (
    "from base import Base, Widget\n\n\n"
    "class Child(Base):\n    pass\n\n\n"
    "def make() -> Widget:\n    return Widget()\n"
)
_INHERITS = ("proj.child.Child", "INHERITS", "proj.base.Base")
_RETURNS = ("proj.child.make", "RETURNS", "proj.base.Widget")


@dataclass
class _Run:
    is_full_build: bool = False
    pending_inbound_edges: list[ResultRow] = field(default_factory=list)


@pytest.fixture
def repo(temp_repo: Path) -> Path:
    (temp_repo / "base.py").write_text(_BASE, encoding="utf-8")
    (temp_repo / "child.py").write_text(_CHILD, encoding="utf-8")
    return temp_repo


def _updater(repo: Path, ingestor: IngestorProtocol) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
        skip_embeddings=True,
    )


def _pass_2(updater: GraphUpdater, *keys: str) -> None:
    updater.factory.structure_processor.identify_structure(emit=False)
    for key in keys:
        updater.file_parser.process_single_file(updater.repo_path / key)


def _deferred(updater: GraphUpdater) -> DeferredResolution:
    return DeferredResolution(
        factory=updater.factory,
        document_tier=updater.document_tier,
        frontend_runner=updater.frontend_runner,
        graph_rehydrator=updater.graph_rehydrator,
        import_closure=updater.import_closure,
        known_module_paths=updater.known_module_paths,
    )


def test_a_parent_parsed_after_its_child_still_resolves(
    repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = _updater(repo, mock_ingestor)
    # Walk order puts the child first: its parent is not registered yet.
    _pass_2(updater, "child.py", "base.py")
    assert _INHERITS not in recorded_triples(mock_ingestor), (
        "fixture guard: Pass 2 must leave inheritance to the deferred stages"
    )

    known = _deferred(updater).resolve_deferred_definitions(
        _Run(is_full_build=True), rehydrate=False, reparsed_keys=set()
    )

    assert {_INHERITS, _RETURNS} <= recorded_triples(mock_ingestor)
    assert set(known) == {"proj.base", "proj.child"}


def test_the_map_returned_is_the_one_the_stages_resolved_against(
    repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = _updater(repo, mock_ingestor)
    _pass_2(updater, "child.py", "base.py")
    handed: list[dict[str, str]] = []

    def known_module_paths() -> dict[str, str]:
        handed.append(updater.known_module_paths())
        return handed[-1]

    deferred = DeferredResolution(
        factory=updater.factory,
        document_tier=updater.document_tier,
        frontend_runner=updater.frontend_runner,
        graph_rehydrator=updater.graph_rehydrator,
        import_closure=updater.import_closure,
        known_module_paths=known_module_paths,
    )

    known = deferred.resolve_deferred_definitions(
        _Run(is_full_build=True), rehydrate=False, reparsed_keys=set()
    )

    # Read once, and the very map handed back for the IMPORTS flush.
    assert len(handed) == 1
    assert known is handed[0]


@pytest.fixture
def embedded_graph(tmp_path: Path) -> Iterator[GraphIngestor]:
    with GraphIngestor(
        connection=GraphConnection(
            dialect=get_dialect(DIALECT_SQLITE),
            sqlite_path=str(tmp_path / "graph.sqlite"),
        )
    ) as ingestor:
        ingestor.ensure_constraints()
        yield ingestor


def _edges(graph: GraphIngestor, *rel_types: str) -> set[tuple[str, str, str]]:
    graph.flush_all()
    rows = graph.fetch_all(
        "MATCH (a)-[r]->(b) WHERE type(r) IN $types "
        "RETURN a.qualified_name AS a, type(r) AS t, b.qualified_name AS b",
        {"types": list(rel_types)},
    )
    return {(str(r["a"]), str(r["t"]), str(r["b"])) for r in rows}


@pytest.fixture
def child_reparsed(repo: Path, embedded_graph: GraphIngestor) -> GraphUpdater:
    """A fresh updater that re-parsed `child.py` alone, over a graph that
    indexed both files and then lost the child's outgoing definition-level
    and import edges, as the delete before an incremental re-parse leaves it."""
    _updater(repo, embedded_graph).run(force=True)
    assert {_INHERITS, _RETURNS} <= _edges(embedded_graph, "INHERITS", "RETURNS"), (
        "fixture guard: the full index must hold both edges"
    )
    embedded_graph.execute_write("MATCH ()-[r:INHERITS|RETURNS|IMPORTS]->() DELETE r")
    updater = _updater(repo, embedded_graph)
    _pass_2(updater, "child.py")
    return updater


def test_an_incremental_run_resolves_into_the_files_it_did_not_reparse(
    child_reparsed: GraphUpdater, embedded_graph: GraphIngestor
) -> None:
    known = _deferred(child_reparsed).resolve_deferred_definitions(
        _Run(), rehydrate=True, reparsed_keys={"child.py"}
    )

    assert _edges(embedded_graph, "INHERITS", "RETURNS") == {_INHERITS, _RETURNS}
    # The unchanged module is known, so the child's import verifies as
    # internal rather than as an external `base`.
    assert "proj.base" in known
    child_reparsed.factory.import_processor.flush_deferred_import_edges(known)
    assert ("proj.child", "IMPORTS", "proj.base") in _edges(embedded_graph, "IMPORTS")


def test_without_the_read_back_nothing_resolves_into_unparsed_files(
    child_reparsed: GraphUpdater, embedded_graph: GraphIngestor
) -> None:
    # A forced run re-parses every file, so it reads nothing back; an
    # unresolvable parent then emits no edge rather than a phantom.
    known = _deferred(child_reparsed).resolve_deferred_definitions(
        _Run(is_full_build=True), rehydrate=False, reparsed_keys={"child.py"}
    )

    assert _edges(embedded_graph, "INHERITS", "RETURNS") == set()
    assert set(known) == {"proj.child"}
