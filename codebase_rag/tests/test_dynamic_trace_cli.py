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


def test_convert_command_fails_cleanly_on_non_utf8_profile(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    profile_path = tmp_path / "invalid.profile"
    profile_path.write_bytes(b"\xff\xfe\x00\x01")

    result = CliRunner().invoke(
        cli,
        ["convert", str(profile_path), "--repo-path", str(repo)],
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_convert_command_sniffs_speedscope_and_requires_include(tmp_path):
    import json as jsonlib

    speedscope = {
        "shared": {
            "frames": [
                {"name": "MyApp!MyApp.Program.Main()"},
                {"name": "MyApp!MyApp.Registry.Handle()"},
            ]
        },
        "profiles": [
            {
                "type": "sampled",
                "samples": [[0, 1]],
                "weights": [3],
            }
        ],
    }
    profile_path = tmp_path / "trace.speedscope.json"
    profile_path.write_text(jsonlib.dumps(speedscope))
    output = tmp_path / "trace.jsonl"

    missing_include = CliRunner().invoke(
        cli, ["convert", str(profile_path), "--output", str(output)]
    )
    assert missing_include.exit_code == 1
    assert "--include" in missing_include.output

    # An include that normalises to nothing (commas/whitespace only) is
    # rejected rather than silently producing an empty trace.
    empty_include = CliRunner().invoke(
        cli,
        ["convert", str(profile_path), "--include", " , ", "--output", str(output)],
    )
    assert empty_include.exit_code == 1
    assert "--include" in empty_include.output

    result = CliRunner().invoke(
        cli,
        [
            "convert",
            str(profile_path),
            "--include",
            "MyApp",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    header, records = read_trace_file(output)
    assert header.language == cs.TRACE_LANGUAGE_DOTNET
    (record,) = list(records)
    assert (record.caller.qualname, record.callee.qualname) == (
        "MyApp.Program.Main",
        "MyApp.Registry.Handle",
    )


def test_convert_command_requires_repo_path_for_cpuprofiles(tmp_path):
    import json as jsonlib

    profile_path = tmp_path / "main.cpuprofile"
    profile_path.write_text(jsonlib.dumps({"nodes": [], "samples": []}))

    result = CliRunner().invoke(cli, ["convert", str(profile_path)])

    assert result.exit_code == 1
    assert "--repo-path" in result.output


def test_convert_command_fails_cleanly_on_unreadable_xt(tmp_path, monkeypatch):
    trace_path = tmp_path / "run.xt"
    trace_path.write_text("File format: 4\n")

    def _deny(*args, **kwargs):
        raise PermissionError(f"denied: {trace_path}")

    monkeypatch.setattr("codebase_rag.trace.xdebug.convert_xdebug_trace", _deny)
    result = CliRunner().invoke(cli, ["convert", str(trace_path)])

    assert result.exit_code == 1
    assert "denied" in result.output


def test_convert_command_sniffs_pprof_and_requires_repo_path(tmp_path):
    import gzip as gziplib

    profile_path = tmp_path / "cpu.out"
    profile_path.write_bytes(gziplib.compress(b"\x00"))

    result = CliRunner().invoke(cli, ["convert", str(profile_path)])

    assert result.exit_code == 1
    assert "--repo-path" in result.output


def test_convert_command_sniffs_addrs_and_requires_repo_path(tmp_path):
    addrs_path = tmp_path / "cgr-trace.addrs"
    addrs_path.write_text("exe /bin/app\nslide 0\n1000 2000 1\n")

    result = CliRunner().invoke(cli, ["convert", str(addrs_path)])

    assert result.exit_code == 1
    assert "--repo-path" in result.output


def test_convert_command_fails_cleanly_on_bad_addrs_number(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    addrs_path = tmp_path / "cgr-trace.addrs"
    addrs_path.write_text("exe /bin/app\nslide 0\nZZZZ 2000 1\n")

    result = CliRunner().invoke(
        cli, ["convert", str(addrs_path), "--repo-path", str(repo)]
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)


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
