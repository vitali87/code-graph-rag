"""A pprof frame keeps a real line when the producer writes no start_line.

pprof-rs (the Rust profiler the tracing guide uses) never sets
`Function.start_line`; it writes the source line on each location's `Line`
record. The decoder read only `start_line`, so every Rust frame had line 0:
same-named functions of one file became one frame, and the resolver bound
each to the alphabetically first name match, so `Lambertian::scatter` and
`Metal::scatter` both resolved to `Dielectric.scatter` (issue #2877).
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_dynamic_trace_rust import (
    _location,
    _msg,
    _sample,
    _string,
    _uint,
)
from codebase_rag.trace.ebpf_pprof import convert_ebpf_pprof
from codebase_rag.trace.pprof import convert_pprof
from codebase_rag.trace.records import FramePoint, read_trace_file
from codebase_rag.trace.resolution import (
    CallableNode,
    FrameResolver,
    ResolutionStats,
)
from codebase_rag.trace.rust_pprof import convert_rust_pprof

_LAMBERTIAN = "<raytracer::Lambertian as raytracer::Material>::scatter"
_METAL = "<raytracer::Metal as raytracer::Material>::scatter"
_RAY_COLOR = "raytracer::ray_color"


def _function(fid: int, name_idx: int, filename_idx: int, start_line: int) -> bytes:
    # pprof-rs writes id, name, system_name and filename only: no start_line.
    payload = _uint(1, fid) + _uint(2, name_idx) + _uint(3, name_idx)
    payload += _uint(4, filename_idx)
    if start_line:
        payload += _uint(5, start_line)
    return _msg(5, payload)


def _issue_profile(root: Path, start_lines: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """The issue's repro: two `scatter` impls under `ray_color`, one location
    per function carrying `Line{function_id, line}`."""
    strings = ["", _LAMBERTIAN, _METAL, _RAY_COLOR, (root / "src/main.rs").as_posix()]
    payload = b""
    for fid, start in zip((1, 2, 3), start_lines, strict=True):
        payload += _function(fid, fid, 4, start)
    for lid, line in ((1, 20), (2, 40), (3, 60)):
        payload += _location(lid, [(lid, line)])
    payload += _sample([1, 3], 5)
    payload += _sample([2, 3], 5)
    for value in strings:
        payload += _string(6, value)
    return payload


def _records(tmp_path: Path, payload: bytes, convert=convert_rust_pprof):
    profile = tmp_path / "profile.pb.gz"
    profile.write_bytes(gzip.compress(payload))
    output = tmp_path / "out.jsonl"
    convert(profile, repo_root=tmp_path, output=output)
    return list(read_trace_file(output)[1])


def test_each_frame_keeps_its_location_line(tmp_path: Path) -> None:
    records = _records(tmp_path, _issue_profile(tmp_path))
    lines = {(r.caller.line, r.callee.line) for r in records}
    assert lines == {(60, 20), (60, 40)}, records


def test_each_impl_resolves_to_its_own_method(tmp_path: Path) -> None:
    records = _records(tmp_path, _issue_profile(tmp_path))
    path = "src/main.rs"
    nodes = [
        CallableNode(cs.NodeLabel.METHOD.value, f"r.main.{t}.scatter", path, s, e)
        for t, s, e in (
            ("Dielectric", 2, 10),
            ("Lambertian", 18, 25),
            ("Metal", 38, 45),
        )
    ] + [CallableNode(cs.NodeLabel.FUNCTION.value, "r.main.ray_color", path, 58, 70)]
    resolver = FrameResolver(tmp_path, nodes)
    stats = ResolutionStats()
    callees = set()
    for record in records:
        resolved = resolver.resolve(record.callee, stats)
        assert resolved is not None, record
        callees.add(resolved.qualified_name)
    assert callees == {"r.main.Lambertian.scatter", "r.main.Metal.scatter"}


def test_one_function_sampled_on_several_lines_is_one_frame(tmp_path: Path) -> None:
    strings = ["", _LAMBERTIAN, _RAY_COLOR, (tmp_path / "src/main.rs").as_posix()]
    payload = _function(1, 1, 3, 0) + _function(2, 2, 3, 0)
    payload += _location(1, [(1, 22)]) + _location(2, [(1, 19)])
    payload += _location(3, [(2, 61)])
    payload += _sample([1, 3], 2) + _sample([2, 3], 3)
    for value in strings:
        payload += _string(6, value)
    records = _records(tmp_path, payload)
    assert len(records) == 1, records
    assert records[0].callee.line == 19
    assert records[0].count == 5


def test_a_producer_start_line_still_wins(tmp_path: Path) -> None:
    # Negative: Go's runtime/pprof writes start_line, the declaration line;
    # a frame keeps it whatever line the sample landed on.
    payload = _issue_profile(tmp_path, start_lines=(17, 37, 57))
    records = _records(tmp_path, payload, convert=convert_pprof)
    assert {r.callee.line for r in records} == {17, 37}, records
    assert {r.caller.line for r in records} == {57}, records


def test_the_ebpf_converter_keeps_the_line_too(tmp_path: Path) -> None:
    records = _records(
        tmp_path,
        _issue_profile(tmp_path),
        convert=lambda p, repo_root, output: convert_ebpf_pprof(
            p, repo_root=repo_root, output=output, language=cs.TRACE_LANGUAGE_RUST
        ),
    )
    assert {(r.caller.line, r.callee.line) for r in records} == {(60, 20), (60, 40)}


@pytest.mark.parametrize("line", [0])
def test_a_location_without_a_line_leaves_the_frame_unlined(
    tmp_path: Path, line: int
) -> None:
    # Negative: a producer that writes neither gives line 0, as before.
    strings = ["", _LAMBERTIAN, _RAY_COLOR, (tmp_path / "src/main.rs").as_posix()]
    payload = _function(1, 1, 3, 0) + _function(2, 2, 3, 0)
    payload += _location(1, [(1, line)]) + _location(2, [(2, line)])
    payload += _sample([1, 2], 1)
    for value in strings:
        payload += _string(6, value)
    records = _records(tmp_path, payload)
    assert records[0].callee == FramePoint(
        path=(tmp_path / "src/main.rs").as_posix(), qualname="scatter", line=0
    )
