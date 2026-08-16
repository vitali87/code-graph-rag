# Rust pprof CPU profiles (pprof-rs / cargo flamegraph) share Go's gzipped
# protobuf wire format, so the converter reuses the decoder but must demangle
# Rust symbols: strip the legacy ::h hash, collapse monomorphised generics and
# trait-qualified receivers to the bare member, and mark closures anonymous.
# The dynamic payoff is dyn Trait dispatch (issue #1251).

from __future__ import annotations

import gzip
import shutil
import subprocess
import sys

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


_CARGO_TOML = """\
[package]
name = "cgrtrace_demo"
version = "0.0.0"
edition = "2021"

[dependencies]
pprof = { version = "0.13", features = ["protobuf-codec"] }

[profile.release]
debug = true
"""

# `dispatch` calls through a `dyn Animal` (dynamic dispatch); `apply` is a
# generic monomorphised for Dog and Cat. Both keep work after the call so the
# callee is not in tail position (else the frame is elided), and #[inline(never)]
# keeps them as distinct frames in the optimised build.
_MAIN_RS = """\
use pprof::protos::Message;

trait Animal { fn speak(&self) -> u64; }
struct Dog;
struct Cat;
impl Animal for Dog {
    #[inline(never)]
    fn speak(&self) -> u64 { let mut a=0u64; for i in 0..8_000_000u64 { a=a.wrapping_add(i%7);} a }
}
impl Animal for Cat {
    #[inline(never)]
    fn speak(&self) -> u64 { let mut a=0u64; for i in 0..8_000_000u64 { a=a.wrapping_add(i%5);} a }
}

#[inline(never)]
fn dispatch(a: &dyn Animal) -> u64 { let r = a.speak(); r.wrapping_add(1) }

#[inline(never)]
fn apply<T: Animal>(a: &T) -> u64 { let r = a.speak(); r.wrapping_add(2) }

fn main() {
    let guard = pprof::ProfilerGuard::new(250).unwrap();
    let mut total = 0u64;
    for i in 0..8 { let a: &dyn Animal = if i%2==0 {&Dog} else {&Cat}; total = total.wrapping_add(dispatch(a)); }
    for _ in 0..4 { total = total.wrapping_add(apply(&Dog)); total = total.wrapping_add(apply(&Cat)); }
    if total == 12345 { println!("{total}"); }
    if let Ok(report) = guard.report().build() {
        let mut f = std::fs::File::create("cpu.pb").unwrap();
        report.pprof().unwrap().write_to_writer(&mut f).unwrap();
    }
}
"""

cargo = shutil.which("cargo")

# Phrases cargo prints only while acquiring dependencies from the registry; any
# one of these is an unambiguous crates.io fetch failure.
_CARGO_FETCH_CONTEXT = (
    "failed to download",
    "failed to get",
    "failed to query replaced source",
    "spurious network error",
    "failed to update registry",
)
# Bare connectivity errors: a build script or a real compile error can also
# print these, so they only count as an outage inside a registry-fetch context.
_CARGO_CONNECTIVITY = (
    "could not resolve host",
    "no such host",
    "connection refused",
    "network failure",
    "timed out",
)


def _is_crates_io_outage(stderr: str) -> bool:
    text = stderr.lower()
    if any(marker in text for marker in _CARGO_FETCH_CONTEXT):
        return True
    in_registry_context = any(
        token in text for token in ("registry", "index", "download")
    )
    return in_registry_context and any(m in text for m in _CARGO_CONNECTIVITY)


@pytest.mark.slow
@pytest.mark.skipif(
    cargo is None or sys.platform != "linux",
    reason="needs cargo; pprof-rs symbolisation is validated on Linux",
)
def test_live_cargo_pprof_captures_dyn_and_generic(tmp_path):
    # A real cargo build + run under pprof-rs: the dyn Trait call and the
    # monomorphised generic must both surface as project edges, with the two
    # generic instantiations collapsed onto the one `apply` definition.
    (tmp_path / "Cargo.toml").write_text(_CARGO_TOML)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text(_MAIN_RS)

    build = subprocess.run(
        [cargo, "build", "--release"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if build.returncode != 0:
        # `pprof` is fetched from crates.io; skip only for a genuine network
        # failure, and fail on a real build error so regressions are not hidden.
        if _is_crates_io_outage(build.stderr):
            pytest.skip(f"crates.io unreachable: {build.stderr[-300:]}")
        raise AssertionError(f"cargo build failed:\n{build.stderr[-1000:]}")
    binary = tmp_path / "target" / "release" / "cgrtrace_demo"
    subprocess.run(
        [str(binary)], cwd=tmp_path, check=True, capture_output=True, timeout=300
    )

    output = tmp_path / "trace.jsonl"
    convert_rust_pprof(tmp_path / "cpu.pb", repo_root=tmp_path, output=output)

    _header, records = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    # The dyn Trait dispatch static analysis cannot resolve.
    assert ("dispatch", "speak") in edges, sorted(edges)
    # apply::<Dog> and apply::<Cat> collapse onto the one generic `apply` node:
    # every speak caller whose name starts with apply must be exactly `apply`.
    apply_callers = {
        caller for caller, callee in edges if callee == "speak" and "apply" in caller
    }
    assert apply_callers == {"apply"}, sorted(apply_callers)
