"""Trace-derived CALLS edges must survive an incremental re-parse (issue #2429).

A runtime observation lives only in the graph: re-parsing a file deletes its
Module subtree and rebuilds what the source says, so before the fix a
one-line comment edit to a traced file dropped every dynamic edge into or out
of it and reverted `trace_confirmed` static edges to `exact`, and
`cgr dead-code` reported the registry-dispatched handlers again. The edges
are now carried by qualified name, graded stale when an endpoint's
definition changed, and dropped only when an endpoint is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.trace.ingest import ingest_trace
from codebase_rag.trace.records import (
    CallRecord,
    FramePoint,
    TraceHeader,
    write_trace_file,
)
from codebase_rag.types_defs import ResultScalar

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_PROJECT = "svc"
# The public property name the docs promise, spelled out so a rename of the
# constant cannot silently change what users query.
_STALE = "dynamic_stale"

_HANDLERS = """def _on_create(x):
    return x + 1


def _on_delete(x):
    return x - 1


HANDLERS = {"create": "_on_create", "delete": "_on_delete"}


def dispatch(event, x):
    return globals()[HANDLERS[event]](x)
"""

_TEST_HANDLERS = """from handlers import dispatch


def test_dispatch():
    assert dispatch("create", 1) == 2 and dispatch("delete", 1) == 0
"""

# Reaches `dispatch` without importing its module: the only edge between the
# two files is the runtime-only one the trace adds.
_RUNNER = """import importlib


def run():
    return getattr(importlib.import_module("handlers"), "dispatch")("create", 1)
"""

_TRACE_PAIRS = (
    (("tests/test_handlers.py", "test_dispatch", 4), ("handlers.py", "dispatch", 12)),
    (("handlers.py", "dispatch", 12), ("handlers.py", "_on_create", 1)),
    (("handlers.py", "dispatch", 12), ("handlers.py", "_on_delete", 5)),
    (("runner.py", "run", 4), ("handlers.py", "dispatch", 12)),
)

type _Props = dict[str, ResultScalar | list[ResultScalar]]
type _Edges = dict[tuple[str, str], list[_Props]]


def _write_repo(tmp_path: Path) -> Path:
    repo = tmp_path / _PROJECT
    (repo / "tests").mkdir(parents=True)
    (repo / "handlers.py").write_text(_HANDLERS, encoding="utf-8")
    (repo / "tests" / "test_handlers.py").write_text(_TEST_HANDLERS, encoding="utf-8")
    (repo / "runner.py").write_text(_RUNNER, encoding="utf-8")
    return repo


def _updater(ingestor: MemgraphIngestor, repo: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=_PROJECT,
        skip_embeddings=True,
    )


def _sync(ingestor: MemgraphIngestor, repo: Path) -> None:
    _updater(ingestor, repo).run(force=False)


def _ingest(ingestor: MemgraphIngestor, repo: Path, trace_path: Path) -> None:
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_PYTHON,
        repo_root=str(repo),
        tracer=cs.TRACE_TOOL_NAME,
        sampled=False,
    )
    records = [
        CallRecord(
            caller=FramePoint(
                path=str(repo / caller[0]), qualname=caller[1], line=caller[2]
            ),
            callee=FramePoint(
                path=str(repo / callee[0]), qualname=callee[1], line=callee[2]
            ),
            count=1,
            workloads=("tests/test_handlers.py::test_dispatch",),
            receiver_types=(),
        )
        for caller, callee in _TRACE_PAIRS
    ]
    write_trace_file(trace_path, header, records)
    ingest_trace(trace_path, ingestor, repo, _PROJECT)


def _calls(ingestor: MemgraphIngestor) -> _Edges:
    rows = ingestor.fetch_all(
        "MATCH (a)-[r:CALLS]->(b) "
        "WHERE a.qualified_name STARTS WITH 'svc.' "
        "RETURN a.name AS caller, b.name AS callee, properties(r) AS props"
    )
    edges: _Edges = {}
    for row in rows:
        props = row["props"]
        assert isinstance(props, dict)
        edges.setdefault((str(row["caller"]), str(row["callee"])), []).append(
            dict(props)
        )
    for sites in edges.values():
        sites.sort(key=lambda props: sorted(map(str, props.items())))
    return edges


def _traced(tmp_path: Path, ingestor: MemgraphIngestor) -> tuple[Path, _Edges]:
    repo = _write_repo(tmp_path)
    _sync(ingestor, repo)
    _ingest(ingestor, repo, tmp_path / "cgr-trace.jsonl")
    return repo, _calls(ingestor)


def _only(edges: _Edges, caller: str, callee: str) -> _Props:
    (props,) = edges[(caller, callee)]
    return props


def test_trace_ingest_leaves_the_expected_edges(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # The baseline every test below edits from: two runtime-only dispatches,
    # a static edge the trace confirmed on both its sites, and a runtime-only
    # edge from a file that does not import handlers.py.
    _repo, edges = _traced(tmp_path, memgraph_ingestor)

    for callee in ("_on_create", "_on_delete"):
        props = _only(edges, "dispatch", callee)
        assert props[cs.KEY_RESOLUTION] == cs.EdgeResolution.DYNAMIC
        assert props[cs.TRACE_PROP_STATIC_MISSED] is True
    confirmed = edges[("test_dispatch", "dispatch")]
    assert len(confirmed) == 2
    for props in confirmed:
        assert props[cs.KEY_RESOLUTION] == cs.EdgeResolution.TRACE_CONFIRMED
        assert props[cs.TRACE_PROP_DYNAMIC] is True
    assert _only(edges, "run", "dispatch")[cs.TRACE_PROP_STATIC_MISSED] is True
    assert all(props[_STALE] is False for sites in edges.values() for props in sites)


def test_comment_edit_keeps_every_trace_edge_through_incremental_sync(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo, before = _traced(tmp_path, memgraph_ingestor)

    with (repo / "handlers.py").open("a", encoding="utf-8") as handle:
        handle.write("# TODO: add update handler\n")
    _sync(memgraph_ingestor, repo)

    # A comment changes no definition, so the graph's CALLS edges, with every
    # property, are exactly what the trace ingest left.
    assert _calls(memgraph_ingestor) == before


def test_comment_edit_keeps_every_trace_edge_through_reingest(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # The watcher and the MCP `reingest` tool take this path, not `run()`.
    repo, before = _traced(tmp_path, memgraph_ingestor)

    with (repo / "handlers.py").open("a", encoding="utf-8") as handle:
        handle.write("# TODO: add update handler\n")
    _updater(memgraph_ingestor, repo).reingest([repo / "handlers.py"])

    assert _calls(memgraph_ingestor) == before


def test_logic_edit_marks_only_the_edges_touching_the_changed_definition_stale(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo, _before = _traced(tmp_path, memgraph_ingestor)

    (repo / "handlers.py").write_text(
        _HANDLERS.replace("return x + 1", "return x + 2"), encoding="utf-8"
    )
    _sync(memgraph_ingestor, repo)
    edges = _calls(memgraph_ingestor)

    changed = _only(edges, "dispatch", "_on_create")
    assert changed[cs.TRACE_PROP_DYNAMIC] is True
    assert changed[cs.KEY_RESOLUTION] == cs.EdgeResolution.DYNAMIC
    assert changed[_STALE] is True
    assert _only(edges, "dispatch", "_on_delete")[_STALE] is False
    assert _only(edges, "run", "dispatch")[_STALE] is False
    for props in edges[("test_dispatch", "dispatch")]:
        assert props[cs.KEY_RESOLUTION] == cs.EdgeResolution.TRACE_CONFIRMED
        assert props[_STALE] is False


def test_callee_edit_marks_an_unchanged_callers_trace_edge_stale(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # runner.py did not change, but the observation its edge records is of
    # the old `dispatch`, so the edge is kept and graded stale.
    repo, _before = _traced(tmp_path, memgraph_ingestor)

    (repo / "handlers.py").write_text(
        _HANDLERS.replace(
            "return globals()[HANDLERS[event]](x)",
            "return globals()[HANDLERS[event]](x) if event else None",
        ),
        encoding="utf-8",
    )
    _sync(memgraph_ingestor, repo)
    edges = _calls(memgraph_ingestor)

    runner = _only(edges, "run", "dispatch")
    assert runner[cs.TRACE_PROP_STATIC_MISSED] is True
    assert runner[_STALE] is True
    for callee in ("_on_create", "_on_delete"):
        assert _only(edges, "dispatch", callee)[_STALE] is True
    for props in edges[("test_dispatch", "dispatch")]:
        assert props[cs.KEY_RESOLUTION] == cs.EdgeResolution.TRACE_CONFIRMED
        assert props[_STALE] is True


def test_call_static_analysis_now_sees_confirms_instead_of_duplicating(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # The dispatch became a direct call: the observation now confirms the
    # static edge the re-parse created instead of sitting beside it as a
    # second, runtime-only edge.
    repo, _before = _traced(tmp_path, memgraph_ingestor)

    (repo / "handlers.py").write_text(
        _HANDLERS.replace(
            "def dispatch(event, x):\n",
            'def dispatch(event, x):\n    if event == "create":\n'
            "        return _on_create(x)\n",
        ),
        encoding="utf-8",
    )
    _sync(memgraph_ingestor, repo)
    edges = _calls(memgraph_ingestor)

    confirmed = _only(edges, "dispatch", "_on_create")
    assert confirmed[cs.KEY_RESOLUTION] == cs.EdgeResolution.TRACE_CONFIRMED
    assert confirmed[cs.TRACE_PROP_STATIC_MISSED] is False
    assert confirmed[cs.TRACE_PROP_DYNAMIC] is True
    assert confirmed[_STALE] is True
    still_dynamic = _only(edges, "dispatch", "_on_delete")
    assert still_dynamic[cs.KEY_RESOLUTION] == cs.EdgeResolution.DYNAMIC
    assert still_dynamic[cs.TRACE_PROP_STATIC_MISSED] is True


def test_removed_endpoint_drops_its_trace_edge(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo, _before = _traced(tmp_path, memgraph_ingestor)

    (repo / "handlers.py").write_text(
        _HANDLERS.replace("def _on_delete(x):\n    return x - 1\n", ""),
        encoding="utf-8",
    )
    _sync(memgraph_ingestor, repo)
    edges = _calls(memgraph_ingestor)

    assert ("dispatch", "_on_delete") not in edges
    assert not memgraph_ingestor.fetch_all(
        "MATCH (n) WHERE n.name = '_on_delete' RETURN n.qualified_name AS qn"
    )
    assert _only(edges, "dispatch", "_on_create")[cs.TRACE_PROP_DYNAMIC] is True


def test_fresh_trace_ingest_clears_the_stale_flag(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo, _before = _traced(tmp_path, memgraph_ingestor)
    (repo / "handlers.py").write_text(
        _HANDLERS.replace("return x + 1", "return x + 2"), encoding="utf-8"
    )
    _sync(memgraph_ingestor, repo)
    assert _only(_calls(memgraph_ingestor), "dispatch", "_on_create")[_STALE] is True

    _ingest(memgraph_ingestor, repo, tmp_path / "cgr-trace-2.jsonl")

    edges = _calls(memgraph_ingestor)
    assert all(props[_STALE] is False for sites in edges.values() for props in sites)


def test_sync_without_trace_data_adds_no_trace_properties(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path)
    _sync(memgraph_ingestor, repo)
    with (repo / "handlers.py").open("a", encoding="utf-8") as handle:
        handle.write("# TODO: add update handler\n")
    _sync(memgraph_ingestor, repo)

    edges = _calls(memgraph_ingestor)
    assert edges[("test_dispatch", "dispatch")]
    trace_keys = {cs.TRACE_PROP_DYNAMIC, cs.TRACE_PROP_STATIC_MISSED, _STALE}
    for sites in edges.values():
        for props in sites:
            assert not trace_keys & props.keys()
            assert props.get(cs.KEY_RESOLUTION) != cs.EdgeResolution.TRACE_CONFIRMED
