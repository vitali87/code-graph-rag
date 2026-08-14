# Trace ingestion must decorate existing CALLS edges with dynamic provenance,
# create runtime-only edges flagged static_missed, aggregate records that
# resolve to the same edge, and stay idempotent across re-ingestion
# (issue #1246).

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.cypher_queries import (
    CYPHER_TRACE_CALLABLES,
    CYPHER_TRACE_EXISTING_CALLS,
)
from codebase_rag.trace.ingest import ingest_trace
from codebase_rag.trace.records import (
    CallRecord,
    FramePoint,
    TraceHeader,
    write_trace_file,
)

_PROJECT = "proj__deadbeef"


class _FakeGraph:
    """Duck-typed TraceGraphProtocol modelling MERGE + existing-calls filtering.

    Edges written via ``ensure_relationship_batch`` persist, and the
    existing-calls query only reports edges without ``static_missed`` set, the
    way ``CYPHER_TRACE_EXISTING_CALLS`` filters them, so re-ingestion tests
    observe the state a prior ingestion left behind.
    """

    def __init__(self, callable_rows, existing_rows):
        self._callable_rows = callable_rows
        self._static_rows = existing_rows
        self.edges = []
        self.flushed = 0

    def _existing_call_rows(self):
        rows = list(self._static_rows)
        for from_spec, _rel, to_spec, props in self.edges:
            if props and props.get(cs.TRACE_PROP_STATIC_MISSED):
                continue
            rows.append({cs.KEY_FROM_QN: from_spec[2], cs.KEY_TO_QN: to_spec[2]})
        return rows

    def fetch_all(self, query, params=None):
        assert params == {cs.KEY_PREFIX: f"{_PROJECT}."}
        if query == CYPHER_TRACE_CALLABLES:
            return self._callable_rows
        if query == CYPHER_TRACE_EXISTING_CALLS:
            return self._existing_call_rows()
        raise AssertionError(f"unexpected query: {query}")

    def execute_write(self, query, params=None):
        raise AssertionError("ingestion must not issue raw writes")

    def ensure_node_batch(self, label, properties):
        raise AssertionError("ingestion must not create nodes")

    def ensure_relationship_batch(self, from_spec, rel_type, to_spec, properties=None):
        self.edges.append((from_spec, rel_type, to_spec, properties))

    def flush_all(self):
        self.flushed += 1


def _callable_row(label, qualified_name, path, start_line, end_line):
    return {
        cs.KEY_LABEL: label,
        cs.KEY_QUALIFIED_NAME: qualified_name,
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: start_line,
        cs.KEY_END_LINE: end_line,
    }


def _graph() -> _FakeGraph:
    rows = [
        _callable_row(
            cs.NodeLabel.FUNCTION,
            f"{_PROJECT}.pkg.registry.handle",
            "pkg/registry.py",
            5,
            7,
        ),
        _callable_row(
            cs.NodeLabel.FUNCTION,
            f"{_PROJECT}.pkg.registry.greet",
            "pkg/registry.py",
            9,
            10,
        ),
        _callable_row(
            cs.NodeLabel.FUNCTION,
            f"{_PROJECT}.pkg.entry.run_all",
            "pkg/entry.py",
            3,
            8,
        ),
    ]
    existing = [
        {
            cs.KEY_FROM_QN: f"{_PROJECT}.pkg.entry.run_all",
            cs.KEY_TO_QN: f"{_PROJECT}.pkg.registry.handle",
        }
    ]
    return _FakeGraph(rows, existing)


def _record(repo: Path, caller, callee, count, workloads=()):
    caller_rel, caller_qual, caller_line = caller
    callee_rel, callee_qual, callee_line = callee
    return CallRecord(
        caller=FramePoint(
            path=str(repo / caller_rel), qualname=caller_qual, line=caller_line
        ),
        callee=FramePoint(
            path=str(repo / callee_rel), qualname=callee_qual, line=callee_line
        ),
        count=count,
        workloads=tuple(workloads),
        receiver_types=(),
    )


def _write_trace(repo: Path, trace_path: Path, records) -> None:
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_PYTHON,
        repo_root=str(repo),
        tracer=cs.TRACE_TOOL_NAME,
    )
    write_trace_file(trace_path, header, records)


def test_ingest_confirms_static_and_flags_missed_edges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(
        repo,
        trace_path,
        [
            _record(
                repo,
                ("pkg/entry.py", "run_all", 3),
                ("pkg/registry.py", "handle", 5),
                4,
                workloads=("t::one",),
            ),
            # Static analysis cannot see the registry dispatch; this edge must
            # be created and flagged.
            _record(
                repo,
                ("pkg/registry.py", "handle", 5),
                ("pkg/registry.py", "greet", 9),
                4,
                workloads=("t::one", "t::two"),
            ),
        ],
    )
    graph = _graph()

    summary = ingest_trace(trace_path, graph, repo, _PROJECT)

    assert summary.records == 2
    assert summary.edges == 2
    assert summary.confirmed_static == 1
    assert summary.static_missed == 1
    assert summary.unresolved == 0
    assert graph.flushed == 1

    by_pair = {(frm[2], to[2]): props for (frm, _rel, to, props) in graph.edges}
    confirmed = by_pair[
        (f"{_PROJECT}.pkg.entry.run_all", f"{_PROJECT}.pkg.registry.handle")
    ]
    missed = by_pair[
        (f"{_PROJECT}.pkg.registry.handle", f"{_PROJECT}.pkg.registry.greet")
    ]
    assert confirmed[cs.TRACE_PROP_DYNAMIC] is True
    assert confirmed[cs.TRACE_PROP_STATIC_MISSED] is False
    assert confirmed[cs.TRACE_PROP_CALL_COUNT] == 4
    assert missed[cs.TRACE_PROP_STATIC_MISSED] is True
    assert missed[cs.TRACE_PROP_WORKLOADS] == ["t::one", "t::two"]
    assert missed[cs.TRACE_PROP_WORKLOAD_COUNT] == 2


def test_ingest_aggregates_same_edge_and_is_idempotent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    # Two records resolving to the same edge (distinct caller lines within the
    # same function) must merge into one write with summed counts.
    _write_trace(
        repo,
        trace_path,
        [
            _record(
                repo,
                ("pkg/entry.py", "run_all", 3),
                ("pkg/registry.py", "handle", 5),
                2,
            ),
            _record(
                repo,
                ("pkg/entry.py", "run_all", 3),
                ("pkg/registry.py", "handle", 5),
                3,
            ),
        ],
    )

    graph = _graph()
    first = ingest_trace(trace_path, graph, repo, _PROJECT)
    second = ingest_trace(trace_path, graph, repo, _PROJECT)

    assert first.edges == second.edges == 1
    assert graph.edges[0] == graph.edges[1]
    (_frm, _rel, _to, props) = graph.edges[0]
    assert props[cs.TRACE_PROP_CALL_COUNT] == 5


def test_reingest_preserves_runtime_only_classification(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    # Static analysis cannot see this registry dispatch, so the first
    # ingestion creates the edge with static_missed. The second ingestion then
    # observes that edge in the graph; it must not mistake it for static
    # confirmation and flip static_missed off.
    _write_trace(
        repo,
        trace_path,
        [
            _record(
                repo,
                ("pkg/registry.py", "handle", 5),
                ("pkg/registry.py", "greet", 9),
                4,
            ),
        ],
    )

    graph = _graph()
    first = ingest_trace(trace_path, graph, repo, _PROJECT)
    second = ingest_trace(trace_path, graph, repo, _PROJECT)

    assert first.static_missed == second.static_missed == 1
    assert second.confirmed_static == 0
    for _frm, _rel, _to, props in graph.edges:
        assert props[cs.TRACE_PROP_STATIC_MISSED] is True


def test_ingest_dispatches_jvm_traces_to_jvm_resolution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    graph = _FakeGraph(
        [
            _callable_row(
                cs.NodeLabel.METHOD,
                f"{_PROJECT}.src.main.java.com.example.Foo.Foo.bar()",
                "src/main/java/com/example/Foo.java",
                5,
                9,
            ),
            _callable_row(
                cs.NodeLabel.METHOD,
                f"{_PROJECT}.src.main.java.com.example.Foo.Foo.run()",
                "src/main/java/com/example/Foo.java",
                11,
                14,
            ),
        ],
        [],
    )
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_JVM,
        repo_root=str(repo),
        tracer=cs.TRACE_TOOL_NAME_JVM,
    )
    # JVM frames carry package-derived paths and binary names; only the JVM
    # resolver can join them onto the path-derived signature-bearing qns.
    write_trace_file(
        trace_path,
        header,
        [
            CallRecord(
                caller=FramePoint(
                    path="com/example/Foo.java", qualname="Foo.run", line=12
                ),
                callee=FramePoint(
                    path="com/example/Foo.java", qualname="Foo.bar", line=6
                ),
                count=3,
                workloads=("suite",),
                receiver_types=("com.example.Foo",),
            )
        ],
    )

    summary = ingest_trace(trace_path, graph, repo, _PROJECT)

    assert summary.edges == 1
    assert summary.unresolved == 0
    ((frm, _rel, to, props),) = graph.edges
    assert frm[2].endswith("Foo.run()")
    assert to[2].endswith("Foo.bar()")
    assert props[cs.TRACE_PROP_CALL_COUNT] == 3


def test_ingest_dispatches_dotnet_traces_to_name_resolution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    graph = _FakeGraph(
        [
            _callable_row(
                cs.NodeLabel.METHOD,
                f"{_PROJECT}.Worker.MyApp.Worker.RunAsync",
                "Worker.cs",
                3,
                20,
            ),
            _callable_row(
                cs.NodeLabel.METHOD,
                f"{_PROJECT}.Worker.MyApp.Worker.Step",
                "Worker.cs",
                22,
                30,
            ),
        ],
        [],
    )
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_DOTNET,
        repo_root="",
        tracer=cs.TRACE_TOOL_NAME_SPEEDSCOPE,
    )
    # .NET frames have no paths or lines; only CLR-name demangling can join.
    write_trace_file(
        trace_path,
        header,
        [
            CallRecord(
                caller=FramePoint(
                    path="", qualname="MyApp.Worker+<RunAsync>d__3.MoveNext", line=0
                ),
                callee=FramePoint(path="", qualname="MyApp.Worker.Step", line=0),
                count=4,
                workloads=("dotnet-test",),
                receiver_types=(),
            )
        ],
    )

    summary = ingest_trace(trace_path, graph, repo, _PROJECT)

    assert summary.edges == 1
    assert summary.unresolved == 0
    ((frm, _rel, to, _props),) = graph.edges
    assert frm[2].endswith("MyApp.Worker.RunAsync")
    assert to[2].endswith("MyApp.Worker.Step")


def test_ingest_dispatches_dart_traces_to_span_resolution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    graph = _FakeGraph(
        [
            _callable_row(
                cs.NodeLabel.METHOD,
                f"{_PROJECT}.lib.registry.Registry.handle",
                "lib/registry.dart",
                8,
                12,
            ),
            _callable_row(
                cs.NodeLabel.FUNCTION,
                f"{_PROJECT}.lib.registry.greet",
                "lib/registry.dart",
                14,
                16,
            ),
        ],
        [],
    )
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_DART,
        repo_root=str(repo),
        tracer="cgr-trace-dart",
    )
    write_trace_file(
        trace_path,
        header,
        [
            CallRecord(
                caller=FramePoint(
                    path=str(repo / "lib/registry.dart"), qualname="handle", line=8
                ),
                callee=FramePoint(
                    path=str(repo / "lib/registry.dart"), qualname="greet", line=14
                ),
                count=5,
                workloads=("dart-run",),
                receiver_types=(),
            )
        ],
    )

    summary = ingest_trace(trace_path, graph, repo, _PROJECT)

    assert summary.edges == 1
    assert summary.unresolved == 0
    ((frm, _rel, to, _props),) = graph.edges
    assert frm[2].endswith("Registry.handle")
    assert to[2].endswith("registry.greet")


def test_ingest_counts_unresolved_frames(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    _write_trace(
        repo,
        trace_path,
        [
            # Caller resolves; callee is a lambda the graph has no node for.
            _record(
                repo,
                ("pkg/entry.py", "run_all", 3),
                ("pkg/entry.py", "run_all.<locals>.<lambda>", 5),
                1,
            ),
        ],
    )
    graph = _graph()

    summary = ingest_trace(trace_path, graph, repo, _PROJECT)

    assert summary.edges == 0
    assert graph.edges == []
    assert summary.resolution.unresolved == {
        cs.TraceUnresolvedReason.SYNTHETIC.value: 1
    }
