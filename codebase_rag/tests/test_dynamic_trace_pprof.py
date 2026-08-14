# Go pprof CPU profiles are gzipped protobufs whose samples reference
# locations, lines, and functions; the converter must decode the wire
# format without dependencies, normalize Go symbol names, and emit
# interchange call records (issue #1250).

from __future__ import annotations

import gzip

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.pprof import convert_pprof
from codebase_rag.trace.records import read_trace_file


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _uint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _msg(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _string(field: int, value: str) -> bytes:
    return _msg(field, value.encode())


def _function(fid, name_idx, filename_idx, start_line):
    return _msg(
        5,
        _uint(1, fid)
        + _uint(2, name_idx)
        + _uint(4, filename_idx)
        + _uint(5, start_line),
    )


def _location(lid, entries):
    payload = _uint(1, lid)
    for function_id, line in entries:
        payload += _msg(4, _uint(1, function_id) + _uint(2, line))
    return _msg(4, payload)


def _sample(location_ids, value):
    payload = b"".join(_uint(1, lid) for lid in location_ids)
    payload += _uint(2, value)
    return _msg(2, payload)


def _profile(tmp_path):
    repo = tmp_path.as_posix()
    strings = [
        "",
        "main.main",
        "main.runAll",
        "main.handle",
        "main.greet",
        "main.runAll.func1",
        "runtime.mapaccess1",
        "main.(*Dog).Sound",
        f"{repo}/main.go",
        f"{repo}/registry.go",
        "/usr/lib/go/src/runtime/map.go",
        f"{repo}/vendor/dep/dep.go",
        "vendor.Dep",
    ]
    idx = {value: position for position, value in enumerate(strings)}
    payload = b""
    # Samples reference locations leaf-first.
    payload += _sample([4, 3, 2, 1], 7)  # greet <- handle <- runAll <- main
    payload += _sample([4, 6, 3, 2, 1], 2)  # runtime frame between
    payload += _sample([5, 2, 1], 3)  # closure under runAll
    payload += _sample([7, 2, 1], 4)  # method frame
    payload += _sample([8, 1], 9)  # vendored callee
    payload += _location(1, [(1, 30)])
    payload += _location(2, [(2, 12)])
    payload += _location(3, [(3, 21)])
    payload += _location(4, [(4, 26)])
    payload += _location(5, [(5, 14)])
    payload += _location(6, [(6, 100)])
    payload += _location(7, [(7, 40)])
    payload += _location(8, [(8, 5)])
    payload += _function(1, idx["main.main"], idx[f"{repo}/main.go"], 28)
    payload += _function(2, idx["main.runAll"], idx[f"{repo}/main.go"], 10)
    payload += _function(3, idx["main.handle"], idx[f"{repo}/registry.go"], 19)
    payload += _function(4, idx["main.greet"], idx[f"{repo}/registry.go"], 24)
    payload += _function(5, idx["main.runAll.func1"], idx[f"{repo}/main.go"], 13)
    payload += _function(
        6, idx["runtime.mapaccess1"], idx["/usr/lib/go/src/runtime/map.go"], 90
    )
    payload += _function(7, idx["main.(*Dog).Sound"], idx[f"{repo}/main.go"], 38)
    payload += _function(8, idx["vendor.Dep"], idx[f"{repo}/vendor/dep/dep.go"], 3)
    for value in strings:
        payload += _string(6, value)
    return payload


def _convert(tmp_path, workload=None):
    profile_path = tmp_path / "cpu.out"
    profile_path.write_bytes(gzip.compress(_profile(tmp_path)))
    output = tmp_path / "trace.jsonl"
    count = convert_pprof(
        profile_path, repo_root=tmp_path, output=output, workload=workload
    )
    header, records = read_trace_file(output)
    return count, header, list(records)


def test_converts_stacks_to_edges_with_sample_weights(tmp_path):
    count, header, records = _convert(tmp_path)

    assert header.language == cs.TRACE_LANGUAGE_GO
    assert count == len(records)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    dispatch = edges[("handle", "greet")]
    assert dispatch.count == 9  # 7 direct + 2 through the runtime frame
    assert dispatch.callee.path.endswith("registry.go")
    # Identity is the declaration line, matching graph start_lines.
    assert dispatch.callee.line == 24
    assert edges[("main", "runAll")].count == 16


def test_runtime_and_vendored_frames_are_out_of_scope(tmp_path):
    _count, _header, records = _convert(tmp_path)

    for record in records:
        assert "runtime" not in record.caller.qualname
        assert "runtime" not in record.callee.qualname
        assert "/vendor/" not in record.caller.path
        assert "/vendor/" not in record.callee.path


def test_closures_and_methods_normalise(tmp_path):
    _count, _header, records = _convert(tmp_path)

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert ("runAll", cs.TRACE_QUALNAME_ANONYMOUS) in edges
    method = edges[("runAll", "Sound")]
    assert method.callee.line == 38


def test_generic_receiver_methods_keep_their_member_name():
    from codebase_rag.trace.pprof import _bare_name

    assert _bare_name("example.com/pkg.(*Cache[go.shape.string]).Get") == "Get"
    assert _bare_name("main.Map[go.shape.int]") == "Map"
    assert _bare_name("main.(*Box[go.shape.[]uint8]).Put") == "Put"
    assert _bare_name("main.runAll.func1") == "<anonymous>"
    assert _bare_name("main.runAll.func1.1") == "<anonymous>"


def test_workload_label_lands_on_every_record(tmp_path):
    _count, _header, records = _convert(tmp_path, workload="go-test")

    assert records
    for record in records:
        assert record.workloads == ("go-test",)


def test_malformed_profile_is_rejected(tmp_path):
    profile_path = tmp_path / "broken.out"
    profile_path.write_bytes(b"not a pprof")

    with pytest.raises(ValueError):
        convert_pprof(profile_path, repo_root=tmp_path, output=tmp_path / "out.jsonl")


def test_truncated_gzip_profile_is_rejected(tmp_path):
    profile_path = tmp_path / "truncated.out"
    profile_path.write_bytes(gzip.compress(b"pprof")[:-3])

    with pytest.raises(ValueError):
        convert_pprof(profile_path, repo_root=tmp_path, output=tmp_path / "out.jsonl")
