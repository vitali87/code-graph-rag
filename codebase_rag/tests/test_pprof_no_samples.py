"""A valid pprof profile with no samples is named as such, not as malformed.

A Go program that runs for less than one sampling period (10 ms at the
default 100 Hz) writes a well-formed CPU profile with a string table and a
mapping but no samples, locations or functions. `cgr trace convert` called
it "not a pprof CPU profile", which sent users looking for a format problem
when the workload simply ended before the first sample (issue #2887).
"""

from __future__ import annotations

import gzip
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from codebase_rag.tests.test_dynamic_trace_pprof import _msg, _string, _uint
from codebase_rag.trace.cli import cli
from codebase_rag.trace.ebpf_pprof import convert_ebpf_pprof
from codebase_rag.trace.pprof import convert_pprof
from codebase_rag.trace.records import TraceFormatError
from codebase_rag.trace.rust_pprof import convert_rust_pprof

NO_SAMPLES = "has no samples"
NOT_PPROF = "is not a pprof CPU profile"


def _empty_profile(path: Path) -> Path:
    """The shape `runtime/pprof` writes for the issue's 1-call program."""
    strings = ["", "samples", "count", "cpu", "nanoseconds", "/tmp/exe/r15"]
    payload = _msg(1, _uint(1, 1) + _uint(2, 2))  # sample_type samples/count
    payload += _msg(1, _uint(1, 3) + _uint(2, 4))  # sample_type cpu/nanoseconds
    payload += _msg(3, _uint(1, 1) + _uint(2, 0x400000) + _uint(5, 5))  # mapping
    payload += b"".join(_string(6, s) for s in strings)
    payload += _msg(11, _uint(1, 3) + _uint(2, 4))  # period_type
    payload += _uint(12, 10_000_000)  # period: 10 ms
    path.write_bytes(gzip.compress(payload))
    return path


@pytest.mark.parametrize(
    "convert", [convert_pprof, convert_rust_pprof, convert_ebpf_pprof]
)
def test_a_profile_without_samples_says_so(
    tmp_path: Path, convert: Callable[..., int]
) -> None:
    profile = _empty_profile(tmp_path / "cpu.out")
    with pytest.raises(TraceFormatError) as exc_info:
        convert(profile, repo_root=tmp_path, output=tmp_path / "t.jsonl")
    message = str(exc_info.value)
    assert NO_SAMPLES in message, message
    assert NOT_PPROF not in message, message
    assert "sampling period" in message, message
    assert not (tmp_path / "t.jsonl").exists()


def test_trace_convert_prints_the_reason(tmp_path: Path) -> None:
    profile = _empty_profile(tmp_path / "cpu.out")
    result = CliRunner().invoke(
        cli,
        ["convert", str(profile), "--repo-path", str(tmp_path), "-o", "t.jsonl"],
    )
    assert result.exit_code == 1, result.output
    assert NO_SAMPLES in result.output
    assert NOT_PPROF not in result.output


@pytest.mark.parametrize(
    "data",
    [
        b"not a pprof",
        # Decodes, but without even a string table: nothing pprof writes.
        # mtime=0: the gzip header otherwise embeds the current time, so the
        # bytes (and a byte-derived test id) differed between xdist workers.
        gzip.compress(_uint(12, 10_000_000), mtime=0),
    ],
    ids=["plain-bytes", "gzip-without-string-table"],
)
def test_input_that_is_not_a_profile_is_still_called_one(
    tmp_path: Path, data: bytes
) -> None:
    # Negative: the old message stays for what really is malformed.
    profile = tmp_path / "broken.out"
    profile.write_bytes(data)
    with pytest.raises(TraceFormatError, match=NOT_PPROF):
        convert_pprof(profile, repo_root=tmp_path, output=tmp_path / "t.jsonl")
