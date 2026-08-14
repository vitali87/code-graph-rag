# `cgr trace ingest` must run the ingestion pipeline against the configured
# graph, print the resolution summary, and fail cleanly on malformed trace
# files (issue #1246).

from __future__ import annotations

from contextlib import contextmanager

from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.trace.cli import cli
from codebase_rag.trace.records import (
    TraceHeader,
    read_trace_file,
    write_trace_file,
)


class _RecordingGraph:
    def __init__(self):
        self.queries = []

    def fetch_all(self, query, params=None):
        self.queries.append(query)
        return []

    def ensure_relationship_batch(self, from_spec, rel_type, to_spec, properties=None):
        raise AssertionError("an empty trace must not write edges")

    def flush_all(self):
        pass


def _patch_graph(monkeypatch, graph):
    @contextmanager
    def _connect(batch_size):
        yield graph

    monkeypatch.setattr("codebase_rag.main.connect_memgraph", _connect)


def _write_header_only_trace(tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_PYTHON,
        repo_root=str(tmp_path),
        tracer=cs.TRACE_TOOL_NAME,
    )
    write_trace_file(trace_path, header, [])
    return trace_path


def test_ingest_command_prints_summary(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = _write_header_only_trace(tmp_path)
    graph = _RecordingGraph()
    _patch_graph(monkeypatch, graph)

    result = CliRunner().invoke(
        cli,
        [
            "ingest",
            str(trace_path),
            "--repo-path",
            str(repo),
            "--project-name",
            "proj",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "records:          0" in result.output
    assert "edges written:    0" in result.output
    assert len(graph.queries) == 2


def test_convert_command_writes_interchange_trace(tmp_path):
    import json as jsonlib

    repo = tmp_path / "repo"
    repo.mkdir()
    profile = {
        "nodes": [
            {
                "id": 1,
                "callFrame": {
                    "functionName": "runAll",
                    "url": (repo / "main.js").as_uri(),
                    "lineNumber": 2,
                    "columnNumber": 0,
                },
                "hitCount": 1,
                "children": [2],
            },
            {
                "id": 2,
                "callFrame": {
                    "functionName": "helper",
                    "url": (repo / "main.js").as_uri(),
                    "lineNumber": 6,
                    "columnNumber": 0,
                },
                "hitCount": 3,
                "children": [],
            },
        ],
        "startTime": 0,
        "endTime": 1,
        "samples": [],
        "timeDeltas": [],
    }
    profile_path = tmp_path / "main.cpuprofile"
    profile_path.write_text(jsonlib.dumps(profile))
    output = tmp_path / "trace.jsonl"

    result = CliRunner().invoke(
        cli,
        [
            "convert",
            str(profile_path),
            "--repo-path",
            str(repo),
            "--output",
            str(output),
            "--workload",
            "suite",
        ],
    )

    assert result.exit_code == 0, result.output
    header, records = read_trace_file(output)
    assert header.language == cs.TRACE_LANGUAGE_JS
    (record,) = list(records)
    assert (record.caller.qualname, record.callee.qualname) == ("runAll", "helper")
    assert record.workloads == ("suite",)
    assert "1" in result.output


def test_convert_command_fails_cleanly_on_malformed_profile(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile_path = tmp_path / "broken.cpuprofile"
    profile_path.write_text("{}")

    result = CliRunner().invoke(
        cli,
        ["convert", str(profile_path), "--repo-path", str(repo)],
    )

    assert result.exit_code == 1
    assert "cpuprofile" in result.output.lower()


def test_ingest_command_fails_cleanly_on_malformed_trace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("not a trace\n")
    _patch_graph(monkeypatch, _RecordingGraph())

    result = CliRunner().invoke(
        cli,
        ["ingest", str(trace_path), "--repo-path", str(repo)],
    )

    assert result.exit_code == 1
    assert "header" in result.output.lower()
