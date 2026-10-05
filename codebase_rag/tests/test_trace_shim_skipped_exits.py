"""The C shim survives exits that never run, and says so (#2264).

`longjmp` and C++ exceptions under clang++ leave functions without calling
`__cyg_profile_func_exit`. The shim's signed depth counter only grew, so
every later call was attributed to a stale parent, and a long enough loop
wrapped the counter negative and wrote before `cgr_stack`. The counter now
saturates, and a trace in which any frame was skipped is marked `unwound`
so the converter refuses it rather than publish stale callers as exact.
Every binary builds under ASan and UBSan, so an out-of-bounds write or a
signed overflow fails the run rather than passing silently.
"""

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
_SANITIZE = ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"]
_ITERATIONS = 20_000

cc = shutil.which("cc")
symbolizer = shutil.which("atos") or shutil.which("addr2line")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        sys.platform == "win32" or cc is None,
        reason="C toolchain unavailable, or Windows/PE (the shim targets ELF/Mach-O)",
    ),
]


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


def _build_and_run(tmp_path: Path, source: str, optimise: str = "-O0") -> Path:
    if not _sanitizers_link(tmp_path):
        pytest.skip("C compiler with ASan/UBSan unavailable")
    (tmp_path / "main.c").write_text(textwrap.dedent(source), encoding="utf-8")
    binary = tmp_path / "app"
    subprocess.run(
        [
            str(cc),
            "-finstrument-functions",
            "-g",
            optimise,
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


def _markers(addrs: Path) -> set[str]:
    lines = addrs.read_text(encoding="utf-8").splitlines()
    return {line for line in lines if line in ("dropped 1", "unwound 1")}


_JUMP = """
#include <setjmp.h>
#include <stdlib.h>

static jmp_buf env;

static void leaf(void) { longjmp(env, 1); }
static void middle(void) { leaf(); }
"""


def test_a_frame_skipped_by_longjmp_marks_the_trace(tmp_path: Path) -> None:
    # `run` lands the jump and returns: its exit finds the skipped frames
    # above it, resynchronises, and marks the trace.
    addrs = _build_and_run(
        tmp_path,
        _JUMP
        + f"""
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

    assert _markers(addrs) == {"unwound 1"}


@pytest.mark.skipif(symbolizer is None, reason="no atos/addr2line to symbolise")
def test_the_converter_refuses_an_unwound_trace(tmp_path: Path) -> None:
    addrs = _build_and_run(
        tmp_path,
        _JUMP
        + """
        static void run(void) {
            if (setjmp(env) == 0) {
                middle();
            }
        }

        int main(void) {
            run();
            return 0;
        }
        """,
    )

    with pytest.raises(TraceFormatError, match="without their exit hook"):
        convert_instrumented(addrs, repo_root=tmp_path, output=tmp_path / "t.jsonl")


def test_a_stale_caller_is_caught_even_when_no_exit_runs(tmp_path: Path) -> None:
    # The jump lands in main, which leaves through exit(): no exit hook ever
    # runs to notice the skipped frames, so only the frame check at the next
    # enter can see that `leaf` is not really `middle`'s caller.
    addrs = _build_and_run(
        tmp_path,
        _JUMP
        + """
        int main(void) {
            volatile int i = 0;
            setjmp(env);
            if (i++ < 3) {
                middle();
            }
            exit(0);
        }
        """,
    )

    assert _markers(addrs) == {"unwound 1"}


def test_a_jump_between_recursive_frames_is_caught_at_the_exit(
    tmp_path: Path,
) -> None:
    # The inner `run` jumps straight back to the outer one, so the outer's
    # exit finds a `run` on top and matches it by address: only the frame
    # check at the exit tells the invocations apart. `main` then calls a
    # function with a large frame, which the enter check alone can miss,
    # and leaves through exit() so no later exit notices either.
    addrs = _build_and_run(
        tmp_path,
        """
        #include <setjmp.h>
        #include <stdlib.h>
        #include <string.h>

        static jmp_buf env;

        static void run(int nested) {
            if (nested) {
                longjmp(env, 1);
            }
            if (setjmp(env) == 0) {
                run(1);
            }
        }

        static int big(void) {
            volatile char pad[1 << 16];
            memset((char *)pad, 1, sizeof pad);
            return pad[100];
        }

        int main(void) {
            run(0);
            int value = big();
            exit(value == 1 ? 0 : 1);
        }
        """,
    )

    assert _markers(addrs) == {"unwound 1"}


def test_depth_past_the_stack_saturates_and_marks_the_trace(tmp_path: Path) -> None:
    # No exit resynchronises the depth, so it climbs past CGR_STACK_MAX and
    # toward the saturation ceiling; the old signed counter would wrap.
    addrs = _build_and_run(
        tmp_path,
        _JUMP
        + f"""
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

    assert _markers(addrs) == {"dropped 1", "unwound 1"}


@pytest.mark.skipif(symbolizer is None, reason="no atos/addr2line to symbolise")
def test_ordinary_recursion_stays_exact(tmp_path: Path) -> None:
    # The frame check must not mistake a live recursive caller for a skipped
    # one: every callee's frame lies below its caller's.
    addrs = _build_and_run(
        tmp_path,
        """
        static int fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
        static int twice(int n) { return fib(n) + fib(n); }

        int main(void) {
            return twice(15) == 1220 ? 0 : 1;
        }
        """,
    )

    assert _markers(addrs) == set()
    output = tmp_path / "trace.jsonl"
    convert_instrumented(addrs, repo_root=tmp_path, output=output)
    _header, records = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname): r.count for r in records}
    # fib(15) makes 1973 calls including itself; twice makes two of them.
    assert edges == {
        ("main", "twice"): 1,
        ("twice", "fib"): 2,
        ("fib", "fib"): 2 * 1972,
    }


def test_the_unwound_message_is_distinct_from_dropped() -> None:
    assert cs.TRACE_ERR_ADDRS_UNWOUND != cs.TRACE_ERR_ADDRS_DROPPED


@pytest.mark.parametrize("optimise", ["-O1", "-O2", "-O3"])
def test_inlined_calls_in_an_optimised_build_stay_unmarked(
    tmp_path: Path, optimise: str
) -> None:
    # The optimiser inlines the recursion but keeps each inlined call's
    # hooks, which then run from the caller's own frame: a level frame must
    # not read as a skipped one, or every optimised build would be refused.
    addrs = _build_and_run(
        tmp_path,
        """
        #include <string.h>

        static int fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
        static int big(int n) {
            volatile char pad[4096];
            memset((char *)pad, n, sizeof pad);
            return pad[7] + (n ? big(n - 1) : 0);
        }
        static int twice(int n) { return fib(n) + fib(n) + big(20); }

        int main(void) {
            return twice(15) > 0 ? 0 : 1;
        }
        """,
        optimise,
    )

    assert _markers(addrs) == set()
