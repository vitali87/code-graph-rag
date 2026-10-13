"""A method invoking its own lambda is not a call from that method to itself.

A lambda body (`lambda$main$0`, Scala `$anonfun$`, a C# `<Main>b__0_0`, a
JS anonymous function) has no node of its own, so trace ingest folds its
frame into the method whose span holds it. For `main -> lambda$main$0` both
frames then land on `main`, and ingest wrote a `dynamic` CALLS edge `main ->
main` flagged `static_missed`: the entry point "called itself", and every
method that runs a lambda it defines inflated the static-miss count (issue
#2709). Genuine recursion, where both frames name the method, stays.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.tests.test_dynamic_trace_ingest import (
    _PROJECT,
    _callable_row,
    _FakeGraph,
)
from codebase_rag.trace.ingest import ingest_trace
from codebase_rag.trace.records import (
    CallRecord,
    FramePoint,
    TraceHeader,
    write_trace_file,
)

_MAIN = f"{_PROJECT}.src.com.acme.Main.Main.main(String[])"
_GREET = f"{_PROJECT}.src.com.acme.Main.Main.greet(String)"
_FACT = f"{_PROJECT}.src.com.acme.Main.Main.fact(int)"
_JAVA = "src/com/acme/Main.java"
_FRAME_PATH = "com/acme/Main.java"


def _jvm_graph(existing: list[tuple[str, str]]) -> _FakeGraph:
    rows = [
        _callable_row(cs.NodeLabel.METHOD, _MAIN, _JAVA, 6, 9),
        _callable_row(cs.NodeLabel.METHOD, _GREET, _JAVA, 11, 13),
        _callable_row(cs.NodeLabel.METHOD, _FACT, _JAVA, 15, 17),
    ]
    return _FakeGraph(rows, [{cs.KEY_FROM_QN: a, cs.KEY_TO_QN: b} for a, b in existing])


def _record(caller: tuple[str, int], callee: tuple[str, int], path: str = _FRAME_PATH):
    return CallRecord(
        caller=FramePoint(path=path, qualname=caller[0], line=caller[1]),
        callee=FramePoint(path=path, qualname=callee[0], line=callee[1]),
        count=1,
        workloads=(),
        receiver_types=(),
    )


def _ingest(tmp_path: Path, language: str, records, graph: _FakeGraph):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    trace = tmp_path / "trace.jsonl"
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=language,
        repo_root=str(repo),
        tracer=cs.TRACE_TOOL_NAME,
    )
    write_trace_file(trace, header, records)
    return ingest_trace(trace, graph, repo, _PROJECT)


def _pairs(graph: _FakeGraph) -> set[tuple[str, str]]:
    return {(frm[2], to[2]) for frm, _rel, to, _props in graph.edges}


def test_the_issue_repro_writes_only_the_real_call(tmp_path: Path) -> None:
    graph = _jvm_graph([(_MAIN, _GREET)])
    summary = _ingest(
        tmp_path,
        cs.TRACE_LANGUAGE_JVM,
        [
            _record(("Main.main", 7), ("Main.lambda$main$0", 7)),
            _record(("Main.lambda$main$0", 7), ("Main.greet", 11)),
        ],
        graph,
    )
    assert (summary.records, summary.edges) == (2, 1)
    assert (summary.confirmed_static, summary.static_missed) == (1, 0)
    assert summary.resolution.unresolved == {"intra_node": 1}
    assert _pairs(graph) == {(_MAIN, _GREET)}


def test_other_folded_bodies_are_not_self_calls(tmp_path: Path) -> None:
    # A Scala anonymous function and an anonymous-class method folded into
    # the method that runs them.
    graph = _jvm_graph([])
    summary = _ingest(
        tmp_path,
        cs.TRACE_LANGUAGE_JVM,
        [
            _record(("Main.main", 7), ("Main.$anonfun$main$1", 8)),
            _record(("Main.main", 8), ("Main$1.run", 8)),
        ],
        graph,
    )
    assert summary.edges == 0
    assert summary.resolution.unresolved == {"intra_node": 2}
    assert not graph.edges


def test_a_csharp_lambda_is_not_a_self_call(tmp_path: Path) -> None:
    worker = f"{_PROJECT}.src.Worker.Ns.Worker.Run()"
    graph = _FakeGraph(
        [_callable_row(cs.NodeLabel.METHOD, worker, "src/Worker.cs", 3, 9)], []
    )
    summary = _ingest(
        tmp_path,
        cs.TRACE_LANGUAGE_DOTNET,
        [
            _record(("Ns.Worker.Run", 0), ("Ns.Worker+<>c.<Run>b__0_0", 0), path=""),
            _record(("Ns.Worker.Run", 0), ("Ns.Worker.Run", 0), path=""),
        ],
        graph,
    )
    # The lambda is skipped; the second record is real recursion.
    assert summary.resolution.unresolved == {"intra_node": 1}
    assert _pairs(graph) == {(worker, worker)}


def test_genuine_recursion_keeps_its_self_edge(tmp_path: Path) -> None:
    # Negatives: both frames name `fact`, so `fact -> fact` is a real call,
    # confirmed when the static graph has it and flagged when it does not;
    # a lambda calling another method still lands on the enclosing method.
    graph = _jvm_graph([(_FACT, _FACT)])
    summary = _ingest(
        tmp_path,
        cs.TRACE_LANGUAGE_JVM,
        [
            _record(("Main.fact", 16), ("Main.fact", 15)),
            _record(("Main.lambda$main$0", 7), ("Main.greet", 11)),
        ],
        graph,
    )
    assert (summary.edges, summary.confirmed_static, summary.static_missed) == (
        2,
        1,
        1,
    )
    assert summary.unresolved == 0
    assert _pairs(graph) == {(_FACT, _FACT), (_MAIN, _GREET)}
