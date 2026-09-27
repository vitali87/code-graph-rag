"""The C shim survives exits that never run (#2264).

`longjmp` and C++ exceptions under clang++ leave functions without calling
`__cyg_profile_func_exit`. The shim's signed depth counter only grew, so
every later call was attributed to a stale parent, and a long enough loop
wrapped the counter negative and wrote before `cgr_stack`. Both tests build
under ASan and UBSan, so an out-of-bounds write or a signed overflow fails
the run rather than passing silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codebase_rag.trace.instrumented import convert_instrumented
from codebase_rag.trace.records import read_trace_file

_SHIM = Path(__file__).resolve().parents[1] / "trace" / "c_agent" / "cgr_trace_shim.c"
_SANITIZE = ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"]
_ITERATIONS = 20_000

cc = shutil.which("cc")
symbolizer = shutil.which("atos") or shutil.which("addr2line")


def _sanitizers_link(tmp_path: Path) -> bool:
    if cc is None:
        return False
    probe = tmp_path / "probe.c"
    probe.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    built = subprocess.run(
        [cc, *_SANITIZE, str(probe), "-o", str(tmp_path / "probe")],
        capture_output=True,
        check=False,
    )
    return built.returncode == 0


def _build_and_run(tmp_path: Path, source: str) -> Path:
    if not _sanitizers_link(tmp_path):
        pytest.skip("C compiler with ASan/UBSan unavailable")
    (tmp_path / "main.c").write_text(textwrap.dedent(source), encoding="utf-8")
    binary = tmp_path / "app"
    subprocess.run(
        [
            str(cc),
            "-finstrument-functions",
            "-g",
            "-O0",
            "-pthread",
            *_SANITIZE,
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
    return addrs


_skip_platform = pytest.mark.skipif(
    sys.platform == "win32" or cc is None,
    reason="C toolchain unavailable, or Windows/PE (the shim targets ELF/Mach-O)",
)


@pytest.mark.slow
@_skip_platform
@pytest.mark.skipif(symbolizer is None, reason="no atos/addr2line to symbolise")
def test_frames_skipped_by_longjmp_are_popped_at_the_next_exit(
    tmp_path: Path,
) -> None:
    addrs = _build_and_run(
        tmp_path,
        f"""
        #include <setjmp.h>

        static jmp_buf env;

        static void leaf(void) {{ longjmp(env, 1); }}
        static void middle(void) {{ leaf(); }}
        static void run(void) {{
            if (setjmp(env) == 0) {{
                middle();
            }}
        }}

        int main(void) {{
            for (int i = 0; i < {_ITERATIONS}; i++) {{
                run();
            }}
            return 0;
        }}
        """,
    )
    output = tmp_path / "trace.jsonl"
    convert_instrumented(addrs, repo_root=tmp_path, output=output)

    _header, records = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname): r.count for r in records}
    assert edges == {
        ("main", "run"): _ITERATIONS,
        ("run", "middle"): _ITERATIONS,
        ("middle", "leaf"): _ITERATIONS,
    }


@pytest.mark.slow
@_skip_platform
def test_depth_past_the_stack_saturates_and_marks_the_trace(tmp_path: Path) -> None:
    # The jump lands in main, which never returns inside the loop, so no exit
    # ever resynchronises the depth: it climbs past CGR_STACK_MAX.
    addrs = _build_and_run(
        tmp_path,
        f"""
        #include <setjmp.h>

        static jmp_buf env;

        static void leaf(void) {{ longjmp(env, 1); }}
        static void middle(void) {{ leaf(); }}

        int main(void) {{
            volatile int i = 0;
            setjmp(env);
            if (i++ < {_ITERATIONS}) {{
                middle();
            }}
            return 0;
        }}
        """,
    )

    assert "dropped 1" in addrs.read_text(encoding="utf-8").splitlines()
