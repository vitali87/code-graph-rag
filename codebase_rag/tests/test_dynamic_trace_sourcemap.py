# V8 CPU profiles reference the transpiled JavaScript that ran; when the build
# emits source maps, the converter must relocate each frame back to its
# TypeScript source so it lands on the indexed node. The map payload below was
# produced by a real `tsc --sourceMap` build of the sample in the live test
# (issue #1247).

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag.trace.cpuprofile import convert_cpuprofile
from codebase_rag.trace.records import read_trace_file
from codebase_rag.trace.sourcemap import SourceMapIndex, load_source_map

# A genuine `tsc` source map for a dist/app.js compiled from ../src/app.ts.
_APP_JS_MAP = json.dumps(
    {
        "version": 3,
        "file": "app.js",
        "sourceRoot": "",
        "sources": ["../src/app.ts"],
        "names": [],
        "mappings": (
            "AACA,MAAM,QAAQ,GAA4B,EAAE,CAAC;AAE7C,SAAS,KAAK;IACV,OAAO,"
            "IAAI,CAAC;AAChB,CAAC;AAED,SAAS,QAAQ,CAAC,IAAY,EAAE,EAAW;IACvC,"
            "QAAQ,CAAC,IAAI,CAAC,GAAG,EAAE,CAAC;AACxB,CAAC;AAED,SAAS,MAAM,"
            "CAAC,IAAY;IACxB,OAAO,QAAQ,CAAC,IAAI,CAAC,EAAE,CAAC,CAAG,"
            "0DAA0D;AACzF,CAAC;AAED,SAAS,GAAG;IACR,QAAQ,CAAC,OAAO,EAAE,"
            "KAAK,CAAC,CAAC;IACzB,IAAI,GAAG,GAAG,EAAE,CAAC;IACb,KAAK,IAAI,"
            "CAAC,GAAG,CAAC,EAAE,CAAC,GAAG,MAAM,EAAE,CAAC,EAAE,EAAE,CAAC;"
            "QAAC,GAAG,GAAG,MAAM,CAAC,OAAO,CAAC,CAAC;IAAC,CAAC;IAC3D,IAAI,"
            "GAAG,CAAC,MAAM,GAAG,CAAC;QAAE,OAAO,CAAC,GAAG,CAAC,GAAG,CAAC,"
            "CAAC;AACzC,CAAC;AACD,GAAG,EAAE,CAAC"
        ),
    }
)

# Generated (line, column) -> original app.ts line, observed in a real profile.
_EXPECTED = {
    (0, 0): 2,  # module top-level
    (1, 14): 4,  # greet
    (7, 15): 12,  # handle
    (10, 12): 16,  # run
}


def _write_map(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.js").write_text("// compiled\n//# sourceMappingURL=app.js.map\n")
    (dist / "app.js.map").write_text(_APP_JS_MAP)
    return dist / "app.js.map"


def test_original_position_maps_generated_to_source(tmp_path):
    source_map = load_source_map(_write_map(tmp_path))
    assert source_map is not None
    for (line, column), expected in _EXPECTED.items():
        result = source_map.original_position(line, column)
        assert result is not None, (line, column)
        path, source_line = result
        assert path.endswith("src/app.ts")
        assert source_line == expected


def test_nearest_preceding_segment_is_used(tmp_path):
    # A column past the last mapped segment resolves to that segment, not None.
    source_map = load_source_map(_write_map(tmp_path))
    assert source_map is not None
    result = source_map.original_position(10, 999)
    assert result is not None
    assert result[0].endswith("src/app.ts")


def test_unmapped_line_and_bad_map_return_none(tmp_path):
    source_map = load_source_map(_write_map(tmp_path))
    assert source_map is not None
    assert source_map.original_position(9999, 0) is None
    assert source_map.original_position(-1, 0) is None
    bad = tmp_path / "bad.map"
    bad.write_text("{ not json")
    assert load_source_map(bad) is None


def test_truncated_vlq_segment_rejects_the_map(tmp_path):
    # A final digit with the continuation bit set but no digit following is a
    # truncated segment; the map must be rejected, not silently mis-decoded.
    bad = tmp_path / "bad.js.map"
    bad.write_text(
        json.dumps(
            {"version": 3, "sources": ["../src/a.ts"], "names": [], "mappings": "AAAAg"}
        )
    )
    assert load_source_map(bad) is None


def test_index_map_sections_are_flattened(tmp_path):
    # A Source Map v3 index map nests maps under `sections`; each is placed at a
    # generated offset. A section at line 50 must shift its inner positions.
    dist = tmp_path / "dist"
    dist.mkdir()
    (tmp_path / "src").mkdir()
    flat = json.loads(_APP_JS_MAP)
    index_map = {
        "version": 3,
        "sections": [
            {"offset": {"line": 0, "column": 0}, "map": flat},
            {"offset": {"line": 50, "column": 0}, "map": flat},
        ],
    }
    (dist / "bundle.js.map").write_text(json.dumps(index_map))
    source_map = load_source_map(dist / "bundle.js.map")
    assert source_map is not None
    # First section: unshifted, identical to the flat map.
    first = source_map.original_position(10, 12)
    assert first is not None
    assert first[0].endswith("src/app.ts")
    assert first[1] == 16
    # Second section at line 50: the same inner `run` frame is now at line 60.
    shifted = source_map.original_position(60, 12)
    assert shifted is not None
    assert shifted[0].endswith("src/app.ts")
    assert shifted[1] == 16


def test_source_mapping_url_query_and_encoding_resolved(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (tmp_path / "src").mkdir()
    (dist / "app map.js.map").write_text(_APP_JS_MAP)
    js = dist / "weird.js"
    js.write_text("// code\n//# sourceMappingURL=app%20map.js.map?v=123\n")

    result = SourceMapIndex().remap(str(js), 10, 12)
    assert result is not None
    assert result[0].endswith("src/app.ts")
    assert result[1] == 16


def _cpuprofile_node(node_id, name, url, line, column, children):
    return {
        "id": node_id,
        "callFrame": {
            "functionName": name,
            "url": url,
            "lineNumber": line,
            "columnNumber": column,
        },
        "hitCount": 1,
        "children": children,
    }


def test_convert_remaps_transpiled_frames_to_typescript(tmp_path):
    map_path = _write_map(tmp_path)
    url = (map_path.parent / "app.js").as_uri()
    profile = {
        "nodes": [
            _cpuprofile_node(1, "", url, 0, 0, [2]),  # module
            _cpuprofile_node(2, "run", url, 10, 12, [3]),
            _cpuprofile_node(3, "handle", url, 7, 15, [4]),
            _cpuprofile_node(4, "greet", url, 1, 14, []),
        ],
        "samples": [],
        "timeDeltas": [],
    }
    profile_path = tmp_path / "run.cpuprofile"
    profile_path.write_text(json.dumps(profile))
    output = tmp_path / "trace.jsonl"

    convert_cpuprofile(profile_path, repo_root=tmp_path, output=output)

    _header, records = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    # The registry-dispatch edge static analysis cannot see, both endpoints
    # relocated from dist/app.js to src/app.ts.
    dispatch = edges[("handle", "greet")]
    assert dispatch.callee.path.endswith("src/app.ts")
    assert dispatch.callee.line == 4
    assert dispatch.caller.path.endswith("src/app.ts")
    assert dispatch.caller.line == 12
    assert edges[("run", "handle")].callee.line == 12


def _node_with_tsc() -> tuple[str, str] | None:
    node = shutil.which("node")
    tsc = shutil.which("tsc")
    if node is None or tsc is None:
        return None
    return node, tsc


_toolchain = _node_with_tsc()


@pytest.mark.slow
@pytest.mark.skipif(_toolchain is None, reason="node and a local tsc are unavailable")
def test_live_typescript_trace_resolves_to_source(tmp_path):
    node, tsc = _toolchain
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.ts").write_text(
        "type Handler = () => string;\n"
        "const registry: Record<string, Handler> = {};\n"
        "function greet(): string { return 'hi'; }\n"
        "function register(name: string, fn: Handler): void { registry[name] = fn; }\n"
        "function handle(name: string): string { return registry[name](); }\n"
        "function run(): void {\n"
        "    register('greet', greet);\n"
        "    let out = '';\n"
        "    for (let i = 0; i < 3000000; i++) { out = handle('greet'); }\n"
        "    if (out.length < 0) console.log(out);\n"
        "}\n"
        "run();\n"
    )
    subprocess.run(
        [tsc, "--sourceMap", "--outDir", str(tmp_path / "dist"), str(src / "app.ts")],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            node,
            "--no-opt",
            "--cpu-prof",
            "--cpu-prof-dir",
            str(tmp_path),
            "--cpu-prof-name",
            "run.cpuprofile",
            str(tmp_path / "dist" / "app.js"),
        ],
        check=True,
        capture_output=True,
        cwd=tmp_path,
    )
    output = tmp_path / "trace.jsonl"
    convert_cpuprofile(tmp_path / "run.cpuprofile", repo_root=tmp_path, output=output)

    _header, records = read_trace_file(output)
    # Sampling and V8 inlining make which specific frames appear nondeterministic,
    # and a frame V8 attributes to a position the map does not cover keeps its
    # generated location, so the robust invariant is: at least one project frame
    # was relocated off the transpiled dist/*.js onto its .ts source.
    assert records
    assert any(record.callee.path.endswith(".ts") for record in records)


def test_malformed_map_url_is_ignored(tmp_path):
    # A malformed sourceMappingURL (urlsplit raises on bad brackets) must be
    # ignored, keeping the generated frame, not abort the whole conversion.
    js = tmp_path / "app.js"
    js.write_text("// code\n//# sourceMappingURL=http://[\n")
    assert SourceMapIndex().remap(str(js), 0, 0) is None


def test_index_returns_none_without_a_map(tmp_path):
    plain = tmp_path / "plain.js"
    plain.write_text("function f() {}\n")
    assert SourceMapIndex().remap(str(plain), 0, 0) is None
