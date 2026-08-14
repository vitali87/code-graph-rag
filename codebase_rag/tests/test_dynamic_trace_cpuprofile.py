# V8 .cpuprofile files (node --cpu-prof) encode observed call stacks as a
# node tree; the converter must turn project-scoped parent/child links into
# interchange call records, seeing through runtime-internal frames, mapping
# 0-based lines to 1-based, and attributing module toplevels (issue #1247).

from __future__ import annotations

import json

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.cpuprofile import convert_cpuprofile
from codebase_rag.trace.records import read_trace_file


def _frame(function_name, url, line):
    return {
        "functionName": function_name,
        "scriptId": "1",
        "url": url,
        "lineNumber": line,
        "columnNumber": 0,
    }


def _node(node_id, frame, children=(), hit_count=0):
    return {
        "id": node_id,
        "callFrame": frame,
        "hitCount": hit_count,
        "children": list(children),
    }


def _profile(tmp_path):
    """(root)->(main toplevel)->runAll->[handle->greet, forEach->callback]."""
    repo = tmp_path.as_posix()
    main = f"file://{repo}/main.js"
    registry = f"file://{repo}/src/registry.js"
    vendored = f"file://{repo}/node_modules/lib/index.js"
    return {
        "nodes": [
            _node(1, _frame("(root)", "", 0), children=[2]),
            _node(2, _frame("", main, 0), children=[3], hit_count=1),
            _node(3, _frame("runAll", main, 2), children=[4, 6, 8], hit_count=2),
            _node(4, _frame("handle", registry, 6), children=[5], hit_count=3),
            # The registry dispatch static analysis cannot see.
            _node(5, _frame("greet", registry, 10), hit_count=7),
            # A runtime-internal frame between two project frames must be
            # walked through, like the JVM agent's stack walk.
            _node(6, _frame("forEach", "node:internal/per_context", 40), children=[7]),
            _node(7, _frame("callback", main, 8), hit_count=4),
            # Vendored code under the repo root stays out of scope.
            _node(8, _frame("vendored", vendored, 1), hit_count=9),
        ],
        "startTime": 0,
        "endTime": 1000,
        "samples": [],
        "timeDeltas": [],
    }


def _convert(tmp_path, workload=None):
    profile_path = tmp_path / "main.cpuprofile"
    profile_path.write_text(json.dumps(_profile(tmp_path)))
    output = tmp_path / "trace.jsonl"
    count = convert_cpuprofile(
        profile_path, repo_root=tmp_path, output=output, workload=workload
    )
    header, records = read_trace_file(output)
    return count, header, list(records)


def test_converts_project_edges_with_one_based_lines(tmp_path):
    count, header, records = _convert(tmp_path)

    assert header.language == cs.TRACE_LANGUAGE_JS
    assert header.repo_root == str(tmp_path)
    assert count == len(records)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    dispatch = edges[("handle", "greet")]
    assert dispatch.caller.path.endswith("src/registry.js")
    assert dispatch.caller.line == 7
    assert dispatch.callee.line == 11


def test_edge_counts_are_callee_subtree_samples(tmp_path):
    _count, _header, records = _convert(tmp_path)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    # handle's subtree: own 3 + greet 7.
    assert edges[("runAll", "handle")].count == 10
    assert edges[("handle", "greet")].count == 7


def test_toplevel_frame_maps_to_module_qualname(tmp_path):
    _count, _header, records = _convert(tmp_path)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    assert (cs.TRACE_QUALNAME_MODULE, "runAll") in edges


def test_runtime_internal_frames_are_walked_through(tmp_path):
    _count, _header, records = _convert(tmp_path)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}

    assert ("runAll", "callback") in edges
    assert not any("forEach" in pair for edge in edges for pair in edge)


def test_vendored_and_internal_frames_never_appear(tmp_path):
    _count, _header, records = _convert(tmp_path)

    for record in records:
        assert "node_modules" not in record.caller.path
        assert "node_modules" not in record.callee.path
        assert not record.caller.path.startswith("node:")
        assert not record.callee.path.startswith("node:")


def test_workload_label_lands_on_every_record(tmp_path):
    _count, _header, records = _convert(tmp_path, workload="suite")

    assert records
    for record in records:
        assert record.workloads == ("suite",)


def test_malformed_profile_is_rejected(tmp_path):
    profile_path = tmp_path / "broken.cpuprofile"
    profile_path.write_text("{}")

    with pytest.raises(ValueError):
        convert_cpuprofile(
            profile_path, repo_root=tmp_path, output=tmp_path / "out.jsonl"
        )
