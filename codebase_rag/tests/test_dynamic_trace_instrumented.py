# The -finstrument-functions shim records exact function-address pairs; the
# converter must symbolise them, scope to the repository, normalise C++
# names, and emit interchange records with true invocation counts
# (issue #1252). The live test compiles and runs a real instrumented binary.

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.instrumented import convert_instrumented
from codebase_rag.trace.records import TraceFormatError, read_trace_file

_SHIM = Path(__file__).resolve().parents[1] / "trace" / "c_agent" / "cgr_trace_shim.c"


def _write_addrs(tmp_path, pairs, exe="/bin/app", slide=4096):
    lines = [f"exe {exe}", f"slide {slide}"]
    lines += [f"{caller:x} {callee:x} {count}" for caller, callee, count in pairs]
    addrs_path = tmp_path / "cgr-trace.addrs"
    addrs_path.write_text("\n".join(lines) + "\n")
    return addrs_path


def test_converts_symbolised_pairs_with_exact_counts(tmp_path):
    repo = tmp_path.as_posix()
    symbols = {
        0x1000: ("main", f"{repo}/main.c", 20),
        0x2000: ("handle", f"{repo}/registry.c", 8),
        0x3000: ("Dog::sound()", f"{repo}/animal.cpp", 12),
        0x4000: ("printf", "/usr/lib/libc/stdio.c", 100),
    }
    addrs_path = _write_addrs(
        tmp_path,
        [(0x1000, 0x2000, 7), (0x2000, 0x3000, 3), (0x2000, 0x4000, 9)],
    )
    output = tmp_path / "trace.jsonl"

    count = convert_instrumented(
        addrs_path,
        repo_root=tmp_path,
        output=output,
        workload="ctest",
        symbolizer=lambda exe, slide, addrs: symbols,
    )

    header, records_iter = read_trace_file(output)
    records = list(records_iter)
    assert header.language == cs.TRACE_LANGUAGE_CPP
    assert count == len(records) == 2
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert edges[("main", "handle")].count == 7
    method = edges[("handle", "sound")]
    assert method.count == 3
    assert method.callee.path.endswith("animal.cpp")
    assert method.callee.line == 12
    for record in records:
        assert record.workloads == ("ctest",)


def test_executable_path_with_spaces_is_preserved(tmp_path):
    repo = tmp_path.as_posix()
    captured: dict[str, object] = {}

    def _record(exe, slide, addrs):
        captured["exe"] = exe
        return {
            0x1000: ("main", f"{repo}/main.c", 3),
            0x2000: ("run", f"{repo}/main.c", 7),
        }

    addrs_path = _write_addrs(
        tmp_path, [(0x1000, 0x2000, 4)], exe="/opt/my app/bin/app"
    )

    count = convert_instrumented(
        addrs_path,
        repo_root=tmp_path,
        output=tmp_path / "trace.jsonl",
        symbolizer=_record,
    )

    assert captured["exe"] == "/opt/my app/bin/app"
    assert count == 1


def test_dropped_marker_rejects_incomplete_trace(tmp_path):
    repo = tmp_path.as_posix()
    addrs_path = tmp_path / "cgr-trace.addrs"
    addrs_path.write_text("exe /bin/app\nslide 0\ndropped 1\n1000 2000 4\n")
    symbols = {0x1000: ("main", f"{repo}/m.c", 3), 0x2000: ("run", f"{repo}/m.c", 7)}

    with pytest.raises(TraceFormatError, match="incomplete"):
        convert_instrumented(
            addrs_path,
            repo_root=tmp_path,
            output=tmp_path / "trace.jsonl",
            symbolizer=lambda exe, slide, addrs: symbols,
        )


def test_malformed_addrs_is_rejected(tmp_path):
    addrs_path = tmp_path / "broken.addrs"
    addrs_path.write_text("nothing here\n")

    with pytest.raises(TraceFormatError):
        convert_instrumented(
            addrs_path,
            repo_root=tmp_path,
            output=tmp_path / "out.jsonl",
            symbolizer=lambda exe, slide, addrs: {},
        )


cc = shutil.which("cc")
atos = shutil.which("atos") or shutil.which("addr2line")


@pytest.mark.slow
@pytest.mark.skipif(
    sys.platform == "win32" or cc is None or atos is None,
    reason="C toolchain unavailable, or Windows/PE (the shim targets ELF/Mach-O)",
)
def test_live_instrumented_binary_produces_registry_dispatch(tmp_path):
    (tmp_path / "main.c").write_text(
        textwrap.dedent("""
            #include <stdio.h>

            static int (*handlers[4])(void);
            static int handler_count = 0;

            static int greet(void) {
                return 42;
            }

            static void reg(int (*fn)(void)) {
                handlers[handler_count++] = fn;
            }

            static int handle(int index) {
                return handlers[index]();
            }

            int main(void) {
                reg(greet);
                int out = 0;
                for (int i = 0; i < 5; i++) {
                    out += handle(0);
                }
                printf("%d\\n", out);
                return 0;
            }
        """)
    )
    binary = tmp_path / "app"
    subprocess.run(
        [
            str(cc),
            "-finstrument-functions",
            "-g",
            "-O0",
            str(tmp_path / "main.c"),
            str(_SHIM),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    addrs = tmp_path / "cgr-trace.addrs"
    subprocess.run(
        [str(binary)],
        check=True,
        capture_output=True,
        env=dict(os.environ, CGR_TRACE_ADDRS=str(addrs)),
        cwd=tmp_path,
    )

    output = tmp_path / "trace.jsonl"
    count = convert_instrumented(addrs, repo_root=tmp_path, output=output)

    assert count > 0
    _header, records_iter = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records_iter}
    dispatch = edges.get(("handle", "greet"))
    assert dispatch is not None, sorted(edges)
    assert dispatch.count == 5
    assert edges[("main", "handle")].count == 5
