# Rust pprof CPU profiles (pprof-rs / cargo flamegraph) share Go's gzipped
# protobuf wire format, so the converter reuses the decoder but must demangle
# Rust symbols: strip the legacy ::h hash, collapse monomorphised generics and
# trait-qualified receivers to the bare member, and mark closures anonymous.
# The dynamic payoff is dyn Trait dispatch (issue #1251).

from __future__ import annotations

import gzip

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.records import read_trace_file
from codebase_rag.trace.rust_pprof import _bare_name, convert_rust_pprof


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
        "myapp::main::h1111111111111111",
        "myapp::run_all::h2222222222222222",
        "myapp::svc::Registry::handle::h3333333333333333",
        "<myapp::svc::Dog as myapp::svc::Animal>::speak::h4444444444444444",
        "myapp::run_all::{{closure}}::h5555555555555555",
        "core::ptr::drop_in_place::h6666666666666666",
        "myapp::util::Cache<alloc::string::String>::get::h7777777777777777",
        "build_script_build::main::h8888888888888888",
        f"{repo}/src/main.rs",
        f"{repo}/src/svc.rs",
        f"{repo}/src/util.rs",
        "/usr/local/rustup/toolchains/std/library/core/src/ptr/mod.rs",
        f"{repo}/target/debug/build/myapp/build_script_build.rs",
    ]
    idx = {value: position for position, value in enumerate(strings)}
    payload = b""
    # Samples reference locations leaf-first.
    payload += _sample([4, 3, 2, 1], 7)  # speak <- handle <- run_all <- main
    payload += _sample([4, 6, 3, 2, 1], 2)  # std frame between handle and speak
    payload += _sample([5, 2, 1], 3)  # closure under run_all
    payload += _sample([7, 3, 2, 1], 4)  # generic method under handle
    payload += _sample([8, 1], 9)  # build-script frame under target/
    payload += _location(1, [(1, 30)])
    payload += _location(2, [(2, 12)])
    payload += _location(3, [(3, 21)])
    payload += _location(4, [(4, 26)])
    payload += _location(5, [(5, 14)])
    payload += _location(6, [(6, 100)])
    payload += _location(7, [(7, 40)])
    payload += _location(8, [(8, 5)])
    payload += _function(1, idx[strings[1]], idx[f"{repo}/src/main.rs"], 1)
    payload += _function(2, idx[strings[2]], idx[f"{repo}/src/main.rs"], 10)
    payload += _function(3, idx[strings[3]], idx[f"{repo}/src/svc.rs"], 19)
    payload += _function(4, idx[strings[4]], idx[f"{repo}/src/svc.rs"], 24)
    payload += _function(5, idx[strings[5]], idx[f"{repo}/src/main.rs"], 13)
    payload += _function(6, idx[strings[6]], idx[strings[12]], 90)
    payload += _function(7, idx[strings[7]], idx[f"{repo}/src/util.rs"], 40)
    payload += _function(8, idx[strings[8]], idx[strings[13]], 3)
    for value in strings:
        payload += _string(6, value)
    return payload


def _convert(tmp_path, workload=None):
    profile_path = tmp_path / "cpu.pb.gz"
    profile_path.write_bytes(gzip.compress(_profile(tmp_path)))
    output = tmp_path / "trace.jsonl"
    count = convert_rust_pprof(
        profile_path, repo_root=tmp_path, output=output, workload=workload
    )
    header, records = read_trace_file(output)
    return count, header, list(records)


def test_converts_stacks_to_edges_with_sample_weights(tmp_path):
    count, header, records = _convert(tmp_path)

    assert header.language == cs.TRACE_LANGUAGE_RUST
    assert header.sampled is True
    assert count == len(records)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert edges[("main", "run_all")].count == 16
    assert edges[("run_all", "handle")].count == 13


def test_dyn_trait_dispatch_edge_is_captured_through_std_frame(tmp_path):
    # The dyn Trait call handle -> Dog::speak is the runtime-only edge static
    # analysis cannot resolve; it must survive the std frame between the two.
    _count, _header, records = _convert(tmp_path)

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    speak = edges[("handle", "speak")]
    assert speak.count == 9  # 7 direct + 2 seen through core::ptr::drop_in_place
    assert speak.callee.path.endswith("svc.rs")
    # Identity is the declaration line, matching graph start_lines.
    assert speak.callee.line == 24


def test_out_of_repo_and_target_frames_are_out_of_scope(tmp_path):
    _count, _header, records = _convert(tmp_path)

    for record in records:
        for frame in (record.caller, record.callee):
            assert "drop_in_place" not in frame.qualname
            assert "/target/" not in frame.path
            assert not frame.path.startswith("/usr/")


def test_closures_and_generics_normalise(tmp_path):
    _count, _header, records = _convert(tmp_path)

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert ("run_all", cs.TRACE_QUALNAME_ANONYMOUS) in edges
    generic = edges[("handle", "get")]
    assert generic.callee.line == 40


def test_bare_name_demangles_rust_symbols():
    assert _bare_name("myapp::svc::Registry::handle::h3333333333333333") == "handle"
    assert _bare_name("<myapp::svc::Dog as myapp::svc::Animal>::speak::h444") == "speak"
    assert _bare_name("myapp::util::Cache<alloc::string::String>::get") == "get"
    assert _bare_name("myapp::util::process::<u32>") == "process"
    assert _bare_name("myapp::run_all::{{closure}}") == cs.TRACE_QUALNAME_ANONYMOUS
    assert (
        _bare_name("myapp::run_all::{{closure}}::h5555") == cs.TRACE_QUALNAME_ANONYMOUS
    )
    assert _bare_name("<myapp::Foo as core::default::Default>::default") == "default"
    assert _bare_name("main") == "main"


def test_workload_label_lands_on_every_record(tmp_path):
    _count, _header, records = _convert(tmp_path, workload="cargo-test")

    assert records
    for record in records:
        assert record.workloads == ("cargo-test",)


def test_malformed_profile_is_rejected(tmp_path):
    profile_path = tmp_path / "broken.pb.gz"
    profile_path.write_bytes(b"not a pprof")

    with pytest.raises(ValueError):
        convert_rust_pprof(
            profile_path, repo_root=tmp_path, output=tmp_path / "out.jsonl"
        )
