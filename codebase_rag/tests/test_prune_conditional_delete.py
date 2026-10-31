"""The conditional purge contract (`cgr prune`, issue #2479).

`delete_project(expected_root=...)` must route to the conditional query with
the root bound under its own key: a param-name typo or a branch removed back
to the unconditional delete would pass the CLI mocks (whose return value is
theirs) and only a live graph would notice. The routing is pinned on a stub,
and the queries themselves run on the embedded engine, the default backend.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import PropertyParams, ResultRow, ResultValue

_ROOT_CHECK = "\nWHERE p.root_path = $expected_root"


@dataclass
class _Calls:
    # What each read answers, in order; a read past the end finds nothing.
    reads: list[list[ResultRow]] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    executed: list[tuple[str, PropertyParams | None]] = field(default_factory=list)
    pruned_resources: bool = False

    @property
    def queries(self) -> list[str]:
        return [query for query, _ in self.executed]


@pytest.fixture
def calls() -> _Calls:
    return _Calls()


@pytest.fixture
def stub(calls: _Calls) -> Iterator[MemgraphIngestor]:
    def _fetch(
        _self: MemgraphIngestor, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        calls.fetched.append(query)
        return calls.reads.pop(0) if calls.reads else []

    def _execute(
        _self: MemgraphIngestor, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        calls.executed.append((query, params))
        return []

    def _prune_resources(_ingestor: MemgraphIngestor) -> None:
        calls.pruned_resources = True

    with (
        patch.object(MemgraphIngestor, "fetch_all", autospec=True, side_effect=_fetch),
        patch.object(
            MemgraphIngestor, "_execute_query", autospec=True, side_effect=_execute
        ),
        patch(
            "codebase_rag.services.graph_service.prune_unanchored_resources",
            side_effect=_prune_resources,
        ),
        patch.object(MemgraphIngestor, "__init__", lambda self: None),
    ):
        yield MemgraphIngestor()


def test_the_conditional_delete_is_the_delete_with_the_root_check() -> None:
    # Built from one template, so the ownership-scoped walk (#2665) can never
    # drift between `delete-project` and `prune`.
    assert cq.CYPHER_DELETE_PROJECT_IF_ROOT.count(_ROOT_CHECK) == 1
    assert (
        cq.CYPHER_DELETE_PROJECT_IF_ROOT.replace(_ROOT_CHECK, "", 1)
        == cq.CYPHER_DELETE_PROJECT
    )


def test_conditional_root_delete_fires_when_the_root_matches(
    stub: MemgraphIngestor, calls: _Calls
) -> None:
    calls.reads = [[{cs.KEY_ROOT_PATH: "/repo"}], []]
    fired = stub.delete_project("proj__1", expected_root="/repo")

    assert fired is True
    first, *_ = calls.executed
    assert first == (
        cq.CYPHER_DELETE_PROJECT_IF_ROOT,
        {cs.KEY_PROJECT_NAME: "proj__1", cs.KEY_EXPECTED_ROOT: "/repo"},
    )
    assert "$expected_root" in first[0]
    assert cq.CYPHER_DELETE_PROJECT_CUT_OFF_UNITS in calls.queries
    assert cq.CYPHER_DELETE_PROJECT not in calls.queries
    assert calls.pruned_resources is True


def test_conditional_root_delete_does_not_fire_on_a_mismatch(
    stub: MemgraphIngestor, calls: _Calls
) -> None:
    calls.reads = [[{cs.KEY_ROOT_PATH: "/elsewhere"}]]
    fired = stub.delete_project("proj__1", expected_root="/repo")

    assert fired is False
    assert calls.executed == []
    assert calls.pruned_resources is False


def test_conditional_root_delete_does_not_fire_for_a_ghost(
    stub: MemgraphIngestor, calls: _Calls
) -> None:
    calls.reads = [[]]
    fired = stub.delete_project("proj__1", expected_root="/repo")

    assert fired is False
    assert calls.executed == []
    assert calls.pruned_resources is False


def test_a_project_still_there_after_the_delete_was_not_deleted(
    stub: MemgraphIngestor, calls: _Calls
) -> None:
    # Repointed between the read and the delete: the delete matched nothing,
    # and the cut-off sweep, which matches by name, must not run over the
    # live project's units.
    calls.reads = [[{cs.KEY_ROOT_PATH: "/repo"}], [{cs.KEY_ROOT_PATH: "/moved"}]]
    fired = stub.delete_project("proj__1", expected_root="/repo")

    assert fired is False
    assert calls.queries == [cq.CYPHER_DELETE_PROJECT_IF_ROOT]
    assert calls.pruned_resources is False


def test_unconditional_delete_ignores_the_root(
    stub: MemgraphIngestor, calls: _Calls
) -> None:
    fired = stub.delete_project("proj__1")

    assert fired is True
    assert calls.fetched == []
    assert calls.executed[0] == (
        cq.CYPHER_DELETE_PROJECT,
        {cs.KEY_PROJECT_NAME: "proj__1"},
    )
    assert cq.CYPHER_DELETE_PROJECT_CUT_OFF_UNITS in calls.queries
    assert calls.pruned_resources is True


# One checkout indexed under two names, `shop` and `old`, sharing the `src`
# Folder and the File under it (#2665), plus `old.v2` nested under `old`.
_GRAPH = """
CREATE (shop:Project {name: 'shop', root_path: '/w'}),
       (old:Project {name: 'old', root_path: '/w'}),
       (v2:Project {name: 'old.v2', root_path: '/v2'}),
       (src:Folder {absolute_path: '/w/src'}),
       (cart:File {absolute_path: '/w/src/cart.py'}),
       (sp:Package {qualified_name: 'shop.src'}),
       (op:Package {qualified_name: 'old.src'}),
       (sm:Module {qualified_name: 'shop.src.cart'}),
       (om:Module {qualified_name: 'old.src.cart'}),
       (vm:Module {qualified_name: 'old.v2.app'}),
       (cut:Module {qualified_name: 'old.cut_off'}),
       (price:Function {qualified_name: 'old.src.cart.price'}),
       (shop)-[:CONTAINS_FOLDER]->(src), (old)-[:CONTAINS_FOLDER]->(src),
       (src)-[:CONTAINS_PACKAGE]->(sp), (src)-[:CONTAINS_PACKAGE]->(op),
       (sp)-[:CONTAINS_FILE]->(cart), (op)-[:CONTAINS_FILE]->(cart),
       (sp)-[:CONTAINS_MODULE]->(sm), (op)-[:CONTAINS_MODULE]->(om),
       (om)-[:DEFINES]->(price), (v2)-[:CONTAINS_MODULE]->(vm)
"""
_KEYS = (
    "MATCH (n) RETURN coalesce(n.qualified_name, n.absolute_path, n.name) AS k "
    "ORDER BY k"
)
_EVERYTHING = [
    "/w/src",
    "/w/src/cart.py",
    "old",
    "old.cut_off",
    "old.src",
    "old.src.cart",
    "old.src.cart.price",
    "old.v2",
    "old.v2.app",
    "shop",
    "shop.src",
    "shop.src.cart",
]
_WITHOUT_OLD = [
    "/w/src",
    "/w/src/cart.py",
    "old.v2",
    "old.v2.app",
    "shop",
    "shop.src",
    "shop.src.cart",
]


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[MemgraphIngestor]:
    with MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(tmp_path / "graph.sqlite"),
    ) as ingestor:
        ingestor.execute_write(_GRAPH)
        yield ingestor


def _keys(graph: MemgraphIngestor) -> list[str]:
    return [str(row["k"]) for row in graph.fetch_all(_KEYS)]


def _residual(graph: MemgraphIngestor, name: str) -> ResultValue:
    rows = graph.fetch_all(
        cq.CYPHER_COUNT_PROJECT_NODES,
        {cs.KEY_PROJECT_NAME: name, cs.KEY_PROJECT_PREFIX: f"{name}."},
    )
    assert len(rows) == 1, rows
    return rows[0][cs.KEY_RESIDUAL_NODES]


def test_a_matching_root_purges_what_the_project_owns(graph: MemgraphIngestor) -> None:
    assert graph.delete_project("old", expected_root="/w") is True

    # The shared Folder and File stay with `shop`, `old.v2` keeps its own
    # module, and the cut-off module under `old`'s name goes with it.
    assert _keys(graph) == _WITHOUT_OLD
    assert _residual(graph, "old") == 0


def test_a_mismatched_root_deletes_nothing(graph: MemgraphIngestor) -> None:
    assert graph.delete_project("old", expected_root="/gone") is False

    assert _keys(graph) == _EVERYTHING


def test_a_ghost_project_deletes_nothing(graph: MemgraphIngestor) -> None:
    assert graph.delete_project("ghost", expected_root="/w") is False

    assert _keys(graph) == _EVERYTHING


def test_the_conditional_query_itself_skips_a_repointed_project(
    graph: MemgraphIngestor,
) -> None:
    # What the WHERE inside the delete buys: a root changed after any read
    # the caller made still stops the purge.
    graph.execute_write(
        cq.CYPHER_DELETE_PROJECT_IF_ROOT,
        {cs.KEY_PROJECT_NAME: "old", cs.KEY_EXPECTED_ROOT: "/gone"},
    )

    assert _keys(graph) == _EVERYTHING


def test_the_residual_count_sees_what_is_left(graph: MemgraphIngestor) -> None:
    # The Project, both Packages/Modules under `old.` but not under `old.v2.`.
    assert _residual(graph, "old") == 4
    assert _residual(graph, "ghost") == 0
