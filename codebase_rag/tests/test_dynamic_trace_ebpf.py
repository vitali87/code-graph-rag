# eBPF continuous-profiler pprof profiles share Go's protobuf wire format but
# add mappings (per-binary build ids), per-sample labels, and build-time paths.
# The converter must re-anchor those paths to the repo, filter by build id and
# label, map a label to workloads, and count unsymbolised locations per mapping
# rather than dropping them (issue #1287).

from __future__ import annotations

import gzip

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.trace.ebpf_pprof import (
    _prefix_matches,
    _reanchor,
    convert_ebpf_pprof,
)
from codebase_rag.trace.records import TraceFormatError, read_trace_file


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


def _string(value: str) -> bytes:
    return _msg(6, value.encode())


def _function(fid: int, name: int, filename: int, start_line: int) -> bytes:
    return _msg(
        5, _uint(1, fid) + _uint(2, name) + _uint(4, filename) + _uint(5, start_line)
    )


def _mapping(mid: int, filename: int, build_id: int) -> bytes:
    return _msg(3, _uint(1, mid) + _uint(5, filename) + _uint(6, build_id))


def _location(lid: int, mapping_id: int, entries: list[tuple[int, int]]) -> bytes:
    payload = _uint(1, lid) + _uint(2, mapping_id)
    for function_id, line in entries:
        payload += _msg(4, _uint(1, function_id) + _uint(2, line))
    return _msg(4, payload)


def _label(key: int, value: int) -> bytes:
    return _msg(3, _uint(1, key) + _uint(2, value))


def _sample(location_ids: list[int], value: int, labels: list[bytes]) -> bytes:
    payload = b"".join(_uint(1, lid) for lid in location_ids)
    payload += _uint(2, value)
    payload += b"".join(labels)
    return _msg(2, payload)


# String table indices.
_S = {
    "": 0,
    "main.handle": 1,
    "main.greet": 2,
    "/build/src/app/service.go": 3,
    "abc123": 4,  # target build id
    "/app/service": 5,  # target mapping filename
    "libc_internal": 6,
    "/usr/lib/libc.so": 7,
    "libc": 8,  # libc build id
    "endpoint": 9,  # label key
    "/api/greet": 10,  # label value
    "service": 11,  # service label key
    "checkout": 12,  # service label value
}


def _profile_bytes() -> bytes:
    strings = b"".join(_string(s) for s in _S)
    functions = (
        _function(1, _S["main.handle"], _S["/build/src/app/service.go"], 10)
        + _function(2, _S["main.greet"], _S["/build/src/app/service.go"], 20)
        + _function(3, _S["libc_internal"], _S["/usr/lib/libc.so"], 1)
    )
    mappings = _mapping(1, _S["/app/service"], _S["abc123"]) + _mapping(
        2, _S["/usr/lib/libc.so"], _S["libc"]
    )
    locations = (
        _location(1, 1, [(1, 12)])  # handle, target binary
        + _location(2, 1, [(2, 22)])  # greet, target binary
        + _location(3, 2, [(3, 5)])  # libc frame, other binary
        + _location(4, 1, [])  # unsymbolised location in the target binary
    )
    endpoint = _label(_S["endpoint"], _S["/api/greet"])
    svc = _label(_S["service"], _S["checkout"])
    samples = (
        # handle -> libc -> greet (leaf-first): the libc frame is seen through.
        _sample([2, 3, 1], 7, [endpoint, svc])
        # handle -> unsymbolised leaf: counted, no edge.
        + _sample([4, 1], 3, [endpoint, svc])
    )
    return strings + functions + mappings + locations + samples


def _convert(tmp_path, **kwargs):
    profile = tmp_path / "cpu.pb.gz"
    profile.write_bytes(gzip.compress(_profile_bytes()))
    output = tmp_path / "trace.jsonl"
    path_map = [("/build/src/", tmp_path.as_posix() + "/src/")]
    kwargs.setdefault("path_map", path_map)
    count = convert_ebpf_pprof(profile, repo_root=tmp_path, output=output, **kwargs)
    header, records = read_trace_file(output)
    return count, header, list(records)


def test_reanchors_paths_and_captures_seen_through_edge(tmp_path):
    count, header, records = _convert(
        tmp_path, build_id="abc123", workload_label="endpoint"
    )

    assert header.language == cs.TRACE_LANGUAGE_GO
    assert header.sampled is True
    assert count == len(records) == 1
    record = records[0]
    # The dyn edge survives the libc frame between the two project frames, and
    # both endpoints re-anchor from /build/src to the repo.
    assert (record.caller.qualname, record.callee.qualname) == ("handle", "greet")
    assert record.count == 7
    assert record.caller.path == (tmp_path / "src/app/service.go").as_posix()
    assert record.callee.path.endswith("src/app/service.go")
    # The chosen label became the workload (production's "which endpoint ran it").
    assert record.workloads == ("/api/greet",)


def test_unsymbolised_locations_are_counted_per_mapping(tmp_path):
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    try:
        _convert(tmp_path, build_id="abc123")
    finally:
        logger.remove(sink_id)

    joined = "\n".join(messages)
    assert "eBPF symbolisation: 1 unsymbolised locations" in joined, joined
    assert "unsymbolised[/app/service]=1" in joined, joined


def test_service_label_filters_samples(tmp_path):
    # A non-matching service selector drops every sample, so no edges remain.
    count, _header, records = _convert(
        tmp_path, build_id="abc123", service=("service", "billing")
    )
    assert count == 0
    assert records == []


def test_unmapped_build_paths_are_reported_not_resolved(tmp_path):
    # Without a --path-map, the /build/src paths never enter the repo; the frames
    # are counted as unmapped and no edge is produced.
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    try:
        count, _header, records = _convert(tmp_path, path_map=[], build_id="abc123")
    finally:
        logger.remove(sink_id)

    assert count == 0
    assert any("unmapped build paths" in message for message in messages), messages


def test_workload_falls_back_to_cli_value_without_a_label(tmp_path):
    _count, _header, records = _convert(
        tmp_path, build_id="abc123", workload="prod-overlay"
    )
    assert records
    for record in records:
        assert record.workloads == ("prod-overlay",)


def test_unsupported_language_is_rejected(tmp_path):
    profile = tmp_path / "cpu.pb.gz"
    profile.write_bytes(gzip.compress(_profile_bytes()))
    with pytest.raises(TraceFormatError):
        convert_ebpf_pprof(
            profile,
            repo_root=tmp_path,
            output=tmp_path / "out.jsonl",
            language="cobol",
        )


def test_malformed_profile_is_rejected(tmp_path):
    profile = tmp_path / "broken.pb.gz"
    profile.write_bytes(gzip.compress(b"not a pprof"))
    with pytest.raises(TraceFormatError):
        convert_ebpf_pprof(profile, repo_root=tmp_path, output=tmp_path / "out.jsonl")


def _write_profile(tmp_path):
    profile = tmp_path / "cpu.pb.gz"
    profile.write_bytes(gzip.compress(_profile_bytes()))
    return profile


def test_cli_convert_format_ebpf_writes_records(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    profile = _write_profile(tmp_path)
    output = tmp_path / "trace.jsonl"
    result = CliRunner().invoke(
        cli,
        [
            "convert",
            str(profile),
            "--format",
            "ebpf",
            "--repo-path",
            str(tmp_path),
            "-o",
            str(output),
            "--path-map",
            f"/build/src/={tmp_path.as_posix()}/src/",
            "--build-id",
            "abc123",
            "--label",
            "endpoint",
        ],
    )
    assert result.exit_code == 0, result.output
    _header, records = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    assert ("handle", "greet") in edges


def test_cli_rejects_unknown_format(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    profile = _write_profile(tmp_path)
    result = CliRunner().invoke(
        cli, ["convert", str(profile), "--format", "perf", "--repo-path", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Unknown --format" in result.output


def test_cli_rejects_malformed_path_map(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    profile = _write_profile(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "convert",
            str(profile),
            "--format",
            "ebpf",
            "--repo-path",
            str(tmp_path),
            "--path-map",
            "no-equals-sign",
        ],
    )
    assert result.exit_code == 1
    assert "Invalid --path-map" in result.output


@pytest.mark.parametrize(
    "option,error", [("--path-map", "path-map"), ("--service", "service")]
)
def test_cli_rejects_empty_option_key(tmp_path, option, error):
    # `--path-map =REPO` would re-anchor every path; `--service =VALUE` selects no
    # label. An empty key must be rejected, not silently accepted.
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    profile = _write_profile(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "convert",
            str(profile),
            "--format",
            "ebpf",
            "--repo-path",
            str(tmp_path),
            option,
            "=value",
        ],
    )
    assert result.exit_code == 1
    assert error in result.output


def test_path_map_matches_only_at_a_component_boundary():
    # /build/src must not swallow /build/src-other: a partial-component match
    # would emit false in-repo frames for a sibling directory.
    assert _prefix_matches("/build/src/app.go", "/build/src")
    assert _prefix_matches("/build/src", "/build/src")
    assert not _prefix_matches("/build/src-other/app.go", "/build/src")
    mapping = [("/build/src", "/repo/src")]
    assert _reanchor("/build/src/app.go", mapping) == "/repo/src/app.go"
    assert _reanchor("/build/src-other/app.go", mapping) == "/build/src-other/app.go"


def _two_frame_profile(caller_path: str, callee_path: str) -> bytes:
    strings = [
        "",
        "main.caller",
        "main.callee",
        caller_path,
        callee_path,
        "/svc",
        "bid",
    ]
    table = b"".join(_string(s) for s in strings)
    functions = _function(1, 1, 3, 10) + _function(2, 2, 4, 20)
    mapping = _mapping(1, 5, 6)
    locations = _location(1, 1, [(1, 12)]) + _location(2, 1, [(2, 22)])
    # leaf-first: callee then caller, so root-first is caller -> callee.
    sample = _sample([2, 1], 5, [])
    return table + functions + mapping + locations + sample


def test_reanchored_path_cannot_escape_the_repository(tmp_path):
    # A profile path with `..` re-anchors under the repo prefix but resolves
    # outside it; the traversal must be rejected, not injected as an edge.
    profile = tmp_path / "cpu.pb.gz"
    profile.write_bytes(
        gzip.compress(
            _two_frame_profile("/build/src/caller.go", "/build/src/../../outside.go")
        )
    )
    output = tmp_path / "trace.jsonl"
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    try:
        count = convert_ebpf_pprof(
            profile,
            repo_root=tmp_path,
            output=output,
            path_map=[("/build/src/", tmp_path.as_posix() + "/src/")],
        )
    finally:
        logger.remove(sink_id)

    assert count == 0
    _header, records = read_trace_file(output)
    for record in records:
        assert "outside" not in record.caller.path
        assert "outside" not in record.callee.path
    assert any("unmapped build paths" in message for message in messages), messages
