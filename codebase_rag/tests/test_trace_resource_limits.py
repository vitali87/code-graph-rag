"""The trace commands' resource limits hold (#2263).

A gzipped profile was inflated whole, so a few MB of compressed zeros grew
to gigabytes past the download cap that counts compressed bytes only; and
`cgr trace pull --timeout` bounded each socket read, so a server trickling
a byte at a time never tripped it.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag.trace import cli as trace_cli
from codebase_rag.trace.pprof import _decompress
from codebase_rag.trace.records import TraceFormatError

_LIMIT = 64 * 1024
_PATH = Path("profile.pb.gz")


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(cs, "TRACE_MAX_DECOMPRESSED_BYTES", _LIMIT)
    return _LIMIT


def _bomb(size: int) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as fh:
        for _ in range(size // 65536):
            fh.write(bytes(65536))
    return buffer.getvalue()


def test_a_gzip_that_expands_past_the_cap_is_refused(small_cap: int) -> None:
    bomb = _bomb(small_cap * 64)
    assert len(bomb) < small_cap

    with pytest.raises(TraceFormatError, match="decompresses to more than"):
        _decompress(bomb, _PATH)


def test_the_default_cap_refuses_a_real_bomb() -> None:
    # 320 MiB of zeros is about 320 KB compressed, well under the download
    # cap, and must fail without ever being held whole.
    bomb = _bomb(cs.TRACE_MAX_DECOMPRESSED_BYTES + 64 * 1024 * 1024)

    with pytest.raises(TraceFormatError, match="decompresses to more than"):
        _decompress(bomb, _PATH)


def test_output_exactly_at_the_cap_is_kept(small_cap: int) -> None:
    payload = bytes(range(256)) * (small_cap // 256)

    assert _decompress(gzip.compress(payload), _PATH) == payload


def test_every_gzip_member_and_nul_padding_is_read(small_cap: int) -> None:
    raw = gzip.compress(b"first ") + gzip.compress(b"second") + bytes(8)

    assert _decompress(raw, _PATH) == b"first second"


def test_many_empty_members_are_refused_quickly() -> None:
    # Each member is ~20 bytes and inflates to nothing, so both byte caps
    # hold; bounded work per member keeps this linear, and past the member
    # cap the file is not a profile.
    raw = gzip.compress(b"") * 200_000
    started = time.monotonic()

    with pytest.raises(TraceFormatError, match="is not a pprof CPU profile"):
        _decompress(raw, _PATH)
    assert time.monotonic() - started < 2.0


def test_members_up_to_the_member_cap_are_read() -> None:
    raw = gzip.compress(b"x") * cs.TRACE_MAX_GZIP_MEMBERS

    assert _decompress(raw, _PATH) == b"x" * cs.TRACE_MAX_GZIP_MEMBERS


def test_a_member_spanning_many_input_slices_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cs, "TRACE_GZIP_INPUT_CHUNK_BYTES", 7)
    payload = bytes(range(256)) * 64
    raw = gzip.compress(payload) + gzip.compress(b"tail")

    assert _decompress(raw, _PATH) == payload + b"tail"


def test_members_together_past_the_cap_are_refused(small_cap: int) -> None:
    half = gzip.compress(bytes(small_cap // 2 + 1))

    with pytest.raises(TraceFormatError, match="decompresses to more than"):
        _decompress(half + half, _PATH)


@pytest.mark.parametrize(
    "raw",
    [
        # mtime=0: a gzip header carries the time it was written, and the
        # parameters must be identical in every xdist worker's collection.
        gzip.compress(b"profile", mtime=0)[:-6],
        b"\x1f\x8b" + b"not deflate at all",
        gzip.compress(b"profile", mtime=0) + b"trailing junk",
    ],
    ids=["truncated", "not-deflate", "trailing-junk"],
)
def test_malformed_gzip_is_refused(raw: bytes) -> None:
    with pytest.raises(TraceFormatError, match="is not a pprof CPU profile"):
        _decompress(raw, _PATH)


def test_an_uncompressed_profile_passes_through() -> None:
    assert _decompress(b"\x0a\x00", _PATH) == b"\x0a\x00"


@contextlib.contextmanager
def _trickle_server(*, in_headers: bool) -> Iterator[tuple[str, threading.Event]]:
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if in_headers:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                filler = b"X-Pad: " + b"a" * 4096
            else:
                self.send_response(200)
                self.send_header("Content-Length", "1000000")
                self.end_headers()
                filler = bytes(4096)
            for byte in filler:
                if stop.is_set():
                    return
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)

        def log_message(self, *_args: str) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/profile.pb.gz", stop
    finally:
        stop.set()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("in_headers", [True, False], ids=["headers", "body"])
def test_a_trickling_server_is_abandoned_at_the_timeout(in_headers: bool) -> None:
    with _trickle_server(in_headers=in_headers) as (url, _stop):
        started = time.monotonic()
        with pytest.raises(trace_cli._ConvertUsageError) as raised:
            trace_cli._download_pprof(url, (), 1.0)
        elapsed = time.monotonic() - started

    assert str(raised.value) == ch.ERR_TRACE_PULL_TIMED_OUT.format(
        url=trace_cli._redact_url(url), timeout=1.0
    )
    assert elapsed < 3.0


def test_a_fetch_error_is_raised_not_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_url: str, _headers: tuple[str, ...], _timeout: float) -> bytes:
        raise ValueError("bad port")

    monkeypatch.setattr(trace_cli, "_fetch_pprof", broken)

    with pytest.raises(ValueError, match="bad port"):
        trace_cli._download_pprof("http://127.0.0.1:1/p", (), 5.0)
