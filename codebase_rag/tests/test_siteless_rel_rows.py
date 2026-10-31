"""A row without a site leaves the located edges between its endpoints alone
(issue #2879).

CALLS, REFERENCES, INSTANTIATES, IMPORTS, LINKS_TO and FLOWS_TO keep one edge
per site: the site properties join the MERGE key. A row carrying none of them
used to MERGE on its endpoints alone, which matched every located edge of the
pair and set its properties, `resolution` among them, on each. A guessed call
then read as `exact`, and `cgr rename` rewrote it without --allow-heuristic.
The row now lands on the pair's site-less edge, which is how `index.bin`
already keeps it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

import codec.schema_pb2 as pb
from codebase_rag import constants as cs
from codebase_rag.cypher_queries import build_trace_confirm_calls_query
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.types_defs import PropertyDict, PropertyValue

_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value
_HEURISTIC: PropertyDict = {
    cs.KEY_LINE: 4,
    cs.KEY_COL: 4,
    cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC.value,
}
_EXACT: PropertyDict = {cs.KEY_RESOLUTION: cs.EdgeResolution.EXACT.value}

# The issue's smallest case: `obj.inner()` binds to `make.inner` by name only,
# and returning the closure writes a site-less `exact` row for the same pair.
_RETURNED_CLOSURE = (
    "def make(obj):\n"
    "    def inner():\n"
    "        return 1\n"
    "    obj.inner()\n"
    "    return inner\n"
)

type CallsEdge = tuple[str, str, PropertyValue, PropertyValue]


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[MemgraphIngestor]:
    with MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(tmp_path / "graph.sqlite"),
    ) as ingestor:
        ingestor.ensure_constraints()
        for qn in ("m.a", "m.b", "m.c"):
            ingestor.ensure_node_batch(_FUNCTION, {cs.KEY_QUALIFIED_NAME: qn})
        ingestor.flush_nodes()
        yield ingestor


def _fn(qn: str) -> tuple[str, str, str]:
    return (_FUNCTION, cs.KEY_QUALIFIED_NAME, qn)


def _call(graph: MemgraphIngestor, to_qn: str, props: PropertyDict) -> None:
    graph.ensure_relationship_batch(_fn("m.a"), _CALLS, _fn(to_qn), dict(props))


def _calls(graph: MemgraphIngestor) -> list[CallsEdge]:
    rows = graph.fetch_all(
        "MATCH (a:Function)-[r:CALLS]->(b:Function) "
        "RETURN a.qualified_name AS src, b.qualified_name AS dst, "
        "r.line AS line, r.resolution AS resolution"
    )
    edges = [(str(r["src"]), str(r["dst"]), r["line"], r["resolution"]) for r in rows]
    return sorted(edges, key=lambda e: (e[0], e[1], e[2] is not None, str(e[2])))


def test_a_row_without_a_site_leaves_a_located_edge_as_it_is(
    graph: MemgraphIngestor,
) -> None:
    _call(graph, "m.b", _HEURISTIC)
    graph.flush_relationships()
    _call(graph, "m.b", _EXACT)
    graph.flush_relationships()

    assert _calls(graph) == [
        ("m.a", "m.b", None, "exact"),
        ("m.a", "m.b", 4, "heuristic"),
    ]


@pytest.mark.parametrize("site_less_first", [True, False])
def test_the_order_within_one_flush_does_not_matter(
    graph: MemgraphIngestor, site_less_first: bool
) -> None:
    rows = [_EXACT, _HEURISTIC] if site_less_first else [_HEURISTIC, _EXACT]
    for props in rows:
        _call(graph, "m.b", props)
    graph.flush_relationships()

    assert _calls(graph) == [
        ("m.a", "m.b", None, "exact"),
        ("m.a", "m.b", 4, "heuristic"),
    ]


def test_rows_without_a_site_share_one_edge_per_pair(graph: MemgraphIngestor) -> None:
    _call(graph, "m.b", _HEURISTIC)
    _call(graph, "m.b", {**_EXACT, "first": 1})
    _call(graph, "m.b", {**_EXACT, "second": 2})
    graph.flush_relationships()
    _call(graph, "m.b", {cs.KEY_RESOLUTION: cs.EdgeResolution.DYNAMIC.value})
    graph.flush_relationships()

    rows = graph.fetch_all(
        "MATCH (:Function)-[r:CALLS]->(:Function) WHERE r.line IS NULL "
        "RETURN r.resolution AS resolution, r.first AS first, r.second AS second"
    )
    # One edge, carrying each row's properties with the latest value winning,
    # as repeated writes to one edge always did.
    assert rows == [{"resolution": "dynamic", "first": 1, "second": 2}]
    assert ("m.a", "m.b", 4, "heuristic") in _calls(graph)


def test_the_flush_still_counts_rows_written(graph: MemgraphIngestor) -> None:
    for line in (34, 189, 195):
        _call(graph, "m.b", {cs.KEY_LINE: line, cs.KEY_COL: 4, **_EXACT})
    graph.flush_relationships()
    # A site-less row onto that pair, one whose callee does not exist, and
    # one new edge written twice.
    for to_qn in ("m.b", "m.missing", "m.c", "m.c"):
        _call(graph, to_qn, _EXACT)

    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        graph.flush_relationships()
    finally:
        logger.remove(handler)

    summaries = [line for line in lines if "relationships (" in line]
    assert len(summaries) == 1, lines
    assert "Flushed 4 relationships (3 successful, 1 failed)." in summaries[0]
    assert any(
        "Failed to create 1 of 4 (Function)-[:CALLS]->(Function) relationships" in line
        for line in lines
    ), lines
    assert [e for e in _calls(graph) if e[2] is None] == [
        ("m.a", "m.b", None, "exact"),
        ("m.a", "m.c", None, "exact"),
    ]


def test_trace_confirmation_writes_its_observation_on_every_site(
    graph: MemgraphIngestor,
) -> None:
    for line in (10, 20):
        _call(graph, "m.b", {cs.KEY_LINE: line, cs.KEY_COL: 4, **_EXACT})
    graph.flush_relationships()
    params: dict[str, PropertyValue] = {
        cs.KEY_FROM_QN: "m.a",
        cs.KEY_TO_QN: "m.b",
        **dict.fromkeys(cs.TRACE_CONFIRMED_PROPS),
    }
    params |= {
        cs.KEY_RESOLUTION: cs.EdgeResolution.TRACE_CONFIRMED.value,
        cs.TRACE_PROP_STATIC_MISSED: False,
        cs.TRACE_PROP_CALL_COUNT: 5,
    }

    graph.execute_write(build_trace_confirm_calls_query(_FUNCTION, _FUNCTION), params)

    rows = graph.fetch_all(
        "MATCH (:Function)-[r:CALLS]->(:Function) RETURN r.line AS line, "
        "r.resolution AS resolution, r.dynamic_call_count AS count, r.col AS col"
    )
    # Both sites carry it, no site-less edge appears, and a key the
    # observation left null keeps the edge's own value.
    assert sorted(rows, key=lambda r: str(r["line"])) == [
        {"line": 10, "resolution": "trace_confirmed", "count": 5, "col": 4},
        {"line": 20, "resolution": "trace_confirmed", "count": 5, "col": 4},
    ]


def _graph_calls(root: Path, sqlite_path: Path) -> set[CallsEdge]:
    parsers, queries = load_parsers()
    with MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(sqlite_path),
    ) as ingestor:
        ingestor.ensure_constraints()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="tiny",
        ).run(force=True)
        return set(_calls(ingestor))


def _index_calls(root: Path, out: Path) -> set[CallsEdge]:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ProtobufFileIngestor(str(out), split_index=False),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="tiny",
    ).run(force=True)
    index = pb.GraphCodeIndex()
    index.ParseFromString((out / cs.PROTOBUF_INDEX_FILE).read_bytes())
    edges: set[CallsEdge] = set()
    for rel in index.relationships:
        if rel.type != pb.Relationship.RelationshipType.CALLS:
            continue
        props = dict(rel.properties)
        line = props.get(cs.KEY_LINE)
        edges.add(
            (
                rel.source_id,
                rel.target_id,
                None if line is None else int(line),
                props.get(cs.KEY_RESOLUTION),
            )
        )
    return edges


def test_the_graph_and_the_index_keep_the_same_calls(tmp_path: Path) -> None:
    root = tmp_path / "tiny"
    root.mkdir()
    (root / "app.py").write_text(_RETURNED_CLOSURE, encoding="utf-8")
    out = tmp_path / "index"
    out.mkdir()

    graph = _graph_calls(root, tmp_path / "graph.sqlite")
    index = _index_calls(root, out)

    pair = ("tiny.app.make", "tiny.app.make.inner")
    # The guessed `obj.inner()` keeps its own resolution in both sinks.
    assert (*pair, 4, "heuristic") in graph
    assert {e for e in graph if e[:2] == pair} == {
        (*pair, 4, "heuristic"),
        (*pair, None, "exact"),
    }
    assert graph == index
