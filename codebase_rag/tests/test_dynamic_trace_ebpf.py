# eBPF continuous-profiler pprof profiles share Go's protobuf wire format but
# add mappings (per-binary build ids), per-sample labels, and build-time paths.
# The converter must re-anchor those paths to the repo, filter by build id and
# label, map a label to workloads, and count unsymbolised locations per mapping
# rather than dropping them (issue #1287).

from __future__ import annotations

import contextlib
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


@contextlib.contextmanager
def _pprof_server(payload: bytes, require_auth: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if require_auth and self.headers.get("Authorization") != require_auth:
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):  # silence per-request logging
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/profile.pb.gz"
    finally:
        server.shutdown()


def test_cli_pull_downloads_and_converts(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    output = tmp_path / "trace.jsonl"
    with _pprof_server(
        gzip.compress(_profile_bytes()), require_auth="Bearer tok"
    ) as url:
        result = CliRunner().invoke(
            cli,
            [
                "pull",
                url,
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
                "--header",
                "Authorization=Bearer tok",
            ],
        )
    assert result.exit_code == 0, result.output
    _header, records = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    assert ("handle", "greet") in edges


def test_cli_pull_saves_profile_when_requested(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    saved = tmp_path / "prod.pb.gz"
    with _pprof_server(gzip.compress(_profile_bytes())) as url:
        result = CliRunner().invoke(
            cli,
            [
                "pull",
                url,
                "--repo-path",
                str(tmp_path),
                "-o",
                str(tmp_path / "trace.jsonl"),
                "--save",
                str(saved),
                "--path-map",
                f"/build/src/={tmp_path.as_posix()}/src/",
                "--build-id",
                "abc123",
            ],
        )
    assert result.exit_code == 0, result.output
    assert saved.is_file()


def test_cli_pull_rejects_non_http_url(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    result = CliRunner().invoke(
        cli, ["pull", "file:///etc/passwd", "--repo-path", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Unsupported URL" in result.output


def test_cli_pull_reports_download_failure(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    # Port 1 refuses immediately, so the failure is reported, not raised.
    result = CliRunner().invoke(
        cli,
        [
            "pull",
            "http://127.0.0.1:1/x",
            "--repo-path",
            str(tmp_path),
            "--timeout",
            "5",
        ],
    )
    assert result.exit_code == 1
    assert "Could not download" in result.output


def test_cli_pull_rejects_save_equal_output(tmp_path):
    from click.testing import CliRunner

    from codebase_rag.trace.cli import cli

    same = tmp_path / "same.jsonl"
    result = CliRunner().invoke(
        cli,
        [
            "pull",
            "http://127.0.0.1:1/x",
            "--repo-path",
            str(tmp_path),
            "-o",
            str(same),
            "--save",
            str(same),
        ],
    )
    assert result.exit_code == 1
    assert "--save and --output" in result.output


def test_pull_download_is_size_capped(tmp_path, monkeypatch):
    import codebase_rag.trace.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_MAX_PROFILE_BYTES", 16)
    with _pprof_server(gzip.compress(_profile_bytes())) as url:
        with pytest.raises(cli_mod._ConvertUsageError):
            cli_mod._download_pprof(url, (), 10.0)


def test_pull_strips_auth_on_redirect(tmp_path):
    # A redirect must not forward the Authorization header to the new location,
    # so a profiler endpoint cannot exfiltrate a bearer token via a 302.
    from codebase_rag.trace.cli import _download_pprof

    payload = gzip.compress(_profile_bytes())
    received: dict[str, str | None] = {}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            received["auth"] = self.headers.get("Authorization")
            received["apikey"] = self.headers.get("X-Api-Key")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    target_url = f"http://127.0.0.1:{target.server_address[1]}/target.pb.gz"

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, *_args):
            return

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=redirector.serve_forever, daemon=True).start()
    redirect_url = f"http://127.0.0.1:{redirector.server_address[1]}/start"
    try:
        data = _download_pprof(
            redirect_url,
            ("Authorization=Bearer secret", "X-Api-Key=vendor-token"),
            10.0,
        )
    finally:
        target.shutdown()
        redirector.shutdown()

    assert data == payload
    # Neither the standard nor the custom credential header crossed the redirect.
    assert received["auth"] is None
    assert received["apikey"] is None


# --- Interpreted-language frames (issue #1287 follow-up) ------------------
#
# The OTel/Parca/Pyroscope eBPF profilers unwind the interpreter stack in the
# kernel and report Python frames as source-level names against the real ``.py``
# file, not as native binary addresses. The shapes below are the two observed in
# real captures on a Python 3.12 workload:
#
#   * perf-map symbolisation:  ``py::Service.handle_request``  (Class.method,
#     ``py::`` prefixed, definition line in Function.start_line)
#   * py-spy-style:            ``leaf_compute``  (bare name, line present)
#
# plus ``<module>`` toplevels and standard-library frames whose file sits
# outside the repository. ``--language python`` emits ``language=python`` so
# ingestion routes the records to Python's existing FrameResolver.

from codebase_rag.trace.ingest import ingest_trace  # noqa: E402


def _py_profile_bytes(src_abs: str) -> bytes:
    strings = [
        "",
        "py::Service.handle_request",  # 1: perf-map Class.method + py:: prefix
        "leaf_compute",  # 2: py-spy bare name
        "<module>",  # 3: module toplevel
        "_find_and_load",  # 4: stdlib frame, out-of-repo file
        src_abs,  # 5: the real in-repo source file
        "<frozen importlib._bootstrap>",  # 6: stdlib "file"
        "endpoint",  # 7: label key
        "/api/checkout",  # 8: label value
        "/opt/py/interp",  # 9: interpreter mapping name
    ]
    idx = {s: i for i, s in enumerate(strings)}
    table = b"".join(_string(s) for s in strings)
    functions = (
        _function(1, idx["py::Service.handle_request"], idx[src_abs], 12)
        + _function(2, idx["leaf_compute"], idx[src_abs], 4)
        + _function(3, idx["<module>"], idx[src_abs], 1)
        + _function(4, idx["_find_and_load"], idx["<frozen importlib._bootstrap>"], 900)
    )
    # A single interpreter mapping with no build id: interpreted frames are
    # filtered by source-path containment, never by this mapping.
    mappings = _mapping(1, idx["/opt/py/interp"], 0)
    locations = (
        _location(1, 1, [(1, 14)])  # handle_request, runtime line 14
        + _location(2, 1, [(2, 5)])  # leaf_compute, runtime line 5
        + _location(3, 1, [(3, 25)])  # <module>, runtime line 25
        + _location(4, 1, [(4, 950)])  # stdlib frame, out-of-repo file
    )
    endpoint = _label(idx["endpoint"], idx["/api/checkout"])
    # Leaf-first: leaf_compute, stdlib(seen-through), handle_request, <module>.
    samples = _sample([2, 4, 1, 3], 9, [endpoint])
    return table + functions + mappings + locations + samples


def _py_convert(tmp_path, **kwargs):
    src = tmp_path / "app" / "service.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x = 1\n", encoding="utf-8")
    profile = tmp_path / "py.pb.gz"
    profile.write_bytes(gzip.compress(_py_profile_bytes(src.as_posix())))
    output = tmp_path / "trace.jsonl"
    kwargs.setdefault("language", cs.TRACE_LANGUAGE_PYTHON)
    count = convert_ebpf_pprof(profile, repo_root=tmp_path, output=output, **kwargs)
    header, records = read_trace_file(output)
    return count, header, list(records), output


def test_interpreted_python_frames_are_shaped_for_the_resolver(tmp_path):
    count, header, records, _ = _py_convert(tmp_path)
    assert header.language == cs.TRACE_LANGUAGE_PYTHON
    assert header.sampled is True
    assert header.tracer == cs.TRACE_TOOL_NAME_EBPF
    src = (tmp_path / "app" / "service.py").as_posix()
    edges = {
        (r.caller.qualname, r.callee.qualname): (r.caller, r.callee) for r in records
    }
    # The stdlib frame is seen through, leaving module -> method -> function.
    assert set(edges) == {
        ("<module>", "Service.handle_request"),
        ("Service.handle_request", "leaf_compute"),
    }
    caller, callee = edges[("<module>", "Service.handle_request")]
    # py:: prefix stripped, Class.method preserved, real file, definition line
    # (Function.start_line, which the resolver's span match anchors on).
    assert callee.qualname == "Service.handle_request"
    assert callee.path == src
    assert callee.line == 12
    assert caller.qualname == cs.TRACE_QUALNAME_MODULE
    # The bare py-spy-style leaf keeps its name and its definition line.
    _, leaf = edges[("Service.handle_request", "leaf_compute")]
    assert leaf.qualname == "leaf_compute"
    assert leaf.line == 4
    assert count == 2


def test_interpreted_frames_ignore_a_native_build_id_filter(tmp_path):
    # A build-id filter selects a native binary's mapping; interpreted frames
    # live in the interpreter's mapping and must survive it (they are filtered
    # by source path instead), or every Python edge would vanish.
    count, _header, records, _ = _py_convert(tmp_path, build_id="native-only-xyz")
    assert count == 2
    assert {(r.caller.qualname, r.callee.qualname) for r in records} == {
        ("<module>", "Service.handle_request"),
        ("Service.handle_request", "leaf_compute"),
    }


def test_interpreted_out_of_repo_stdlib_frame_is_counted_not_resolved(tmp_path):
    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO", format="{message}")
    try:
        _count, _header, records, _ = _py_convert(tmp_path)
    finally:
        logger.remove(sink)
    # No edge references the stdlib frame's out-of-repo file.
    assert all("_find_and_load" not in r.callee.qualname for r in records)
    assert all("_find_and_load" not in r.caller.qualname for r in records)
    # The out-of-repo frame is counted and reported, not silently dropped.
    assert any("unmapped build paths" in m for m in messages)


class _FakePyGraph:
    """Minimal TraceGraphProtocol: serves Python callables, records edges."""

    def __init__(self, callable_rows, existing_rows):
        self._callable_rows = callable_rows
        self._existing_rows = existing_rows
        self.edges = []

    def fetch_all(self, query, params=None):
        from codebase_rag.cypher_queries import (
            CYPHER_TRACE_CALLABLES,
            CYPHER_TRACE_EXISTING_CALLS,
        )

        if query == CYPHER_TRACE_CALLABLES:
            return self._callable_rows
        if query == CYPHER_TRACE_EXISTING_CALLS:
            return self._existing_rows
        raise AssertionError(f"unexpected query: {query}")

    def ensure_relationship_batch(self, from_spec, rel_type, to_spec, properties=None):
        self.edges.append((from_spec[2], to_spec[2], properties))

    def flush_all(self):
        return None


def test_interpreted_python_trace_ingests_to_calls_edges(tmp_path):
    # End-to-end proof: convert a Python eBPF profile, then ingest it through the
    # real resolver and confirm the source-level frames bind to graph nodes.
    project = "svc__deadbeef"

    def row(label, qn, start, end):
        return {
            cs.KEY_LABEL: label,
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_PATH: "app/service.py",
            cs.KEY_START_LINE: start,
            cs.KEY_END_LINE: end,
        }

    callables = [
        row(cs.NodeLabel.MODULE, f"{project}.app.service", None, None),
        row(
            cs.NodeLabel.METHOD, f"{project}.app.service.Service.handle_request", 12, 18
        ),
        row(cs.NodeLabel.FUNCTION, f"{project}.app.service.leaf_compute", 4, 6),
    ]
    _count, _header, _records, output = _py_convert(tmp_path)
    graph = _FakePyGraph(callables, [])
    summary = ingest_trace(output, graph, tmp_path, project)

    assert summary.unresolved == 0
    resolved = {(frm, to) for frm, to, _props in graph.edges}
    assert resolved == {
        (f"{project}.app.service", f"{project}.app.service.Service.handle_request"),
        (
            f"{project}.app.service.Service.handle_request",
            f"{project}.app.service.leaf_compute",
        ),
    }
    # Runtime-only edges from a sampled profile carry the sampled provenance flag.
    assert all(props[cs.TRACE_PROP_SAMPLED] for _f, _t, props in graph.edges)
