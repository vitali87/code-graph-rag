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
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.trace.instrumented import _bare_name, convert_instrumented
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
    assert header.sampled is False
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


def test_unresolved_addresses_are_reported(tmp_path):
    # An address that symbolises to no usable source position must be reported,
    # not silently dropped. Two failure modes: no name at all (`??`), and a
    # valid name with no file/line because debug info was stripped.
    repo = tmp_path.as_posix()
    symbols = {
        0x1000: ("main", f"{repo}/main.c", 3),
        0x2000: ("run", f"{repo}/main.c", 7),
        0x3000: ("??", "??", 0),
        0x4000: ("stripped_fn", "", 0),
    }
    addrs_path = _write_addrs(
        tmp_path, [(0x1000, 0x2000, 4), (0x2000, 0x3000, 2), (0x2000, 0x4000, 1)]
    )

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        count = convert_instrumented(
            addrs_path,
            repo_root=tmp_path,
            output=tmp_path / "trace.jsonl",
            symbolizer=lambda _e, _s, _a: symbols,
        )
    finally:
        logger.remove(sink_id)

    # Only main -> run survives; both the ?? and the position-less named frame
    # are dropped, and both are counted in the warning (2 of 4 addresses).
    assert count == 1
    assert any(
        "2 of 4" in message and "did not symbolise" in message for message in messages
    )


def test_bare_name_collapses_templates_and_demangles():
    # `addr2line -f -C` prints a template instantiation with its return type and
    # its instantiated arguments (`int apply<Dog>(Dog const*)`); every
    # instantiation must collapse to the one source definition so they share a
    # node, methods drop their qualifier, and operators are kept whole.
    assert _bare_name("int apply<Dog>(Dog const*)") == "apply"
    assert _bare_name("int apply<Cat>(Cat const*)") == "apply"
    assert _bare_name("int Cache::get<int>(int)") == "get"
    assert _bare_name("std::vector<int> make<Dog>(Dog const&)") == "make"
    assert _bare_name("unsigned int apply<Dog, Cat>(int)") == "apply"
    assert _bare_name("Reg::handle(int)") == "handle"
    assert _bare_name("Dog::sound(int)") == "sound"
    assert _bare_name("ns::sub::foo(int)") == "foo"
    assert _bare_name("dispatch(Animal const*)") == "dispatch"
    assert _bare_name("main") == "main"
    # A trailing const/ref qualifier is dropped, not mistaken for the name.
    assert _bare_name("Reg::size(int) const") == "size"


def test_bare_name_keeps_complete_operator_names():
    # `addr2line -f -C` spells operators in full (including the trailing const
    # cv-qualifier and the parameter list); the whole operator name must survive,
    # never truncated to the first token.
    assert _bare_name("F::operator<(F const&, F const&)") == "operator<"
    assert _bare_name("bool ns::operator<<(A const&)") == "operator<<"
    assert _bare_name("F::operator<(F const&) const") == "operator<"
    # Combined trailing qualifiers are all stripped (and the shrinking loop that
    # does it cannot backtrack, unlike the earlier regex).
    assert _bare_name("F::at(int) const && noexcept") == "at"
    assert _bare_name("F::operator()(int)") == "operator()"
    assert _bare_name("F::operator[](int)") == "operator[]"
    assert _bare_name("operator new[](unsigned long, A&)") == "operator new[]"
    assert _bare_name("operator new(unsigned long)") == "operator new"
    assert _bare_name('operator"" _tag(unsigned long long)') == 'operator"" _tag'
    # A qualified conversion operator keeps its complete conversion-type spelling,
    # including the `::` inside it, rather than truncating at the first token.
    assert _bare_name("X::operator std::string() const") == "operator std::string"
    assert (
        _bare_name("F::operator std::__cxx11::basic_string<char> () const")
        == "operator std::__cxx11::basic_string<char>"
    )
    assert _bare_name("") == cs.TRACE_QUALNAME_ANONYMOUS


cc = shutil.which("cc")
cxx = shutil.which("c++") or shutil.which("g++")
atos = shutil.which("atos") or shutil.which("addr2line")
cmake = shutil.which("cmake")


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


@pytest.mark.slow
@pytest.mark.skipif(
    sys.platform == "win32" or cc is None or cxx is None or atos is None,
    reason="C/C++ toolchain unavailable, or Windows/PE (the shim targets ELF/Mach-O)",
)
def test_live_cpp_virtual_dispatch_produces_virtual_edge(tmp_path):
    (tmp_path / "main.cpp").write_text(
        textwrap.dedent("""
            struct Animal {
                virtual ~Animal() = default;
                virtual int speak() = 0;
            };
            struct Dog : Animal {
                int speak() override;
            };
            int Dog::speak() { return 7; }

            static int dispatch(Animal* a) {
                return a->speak();     // virtual call through the trait object
            }

            int main() {
                Dog d;
                int out = 0;
                for (int i = 0; i < 5; i++) {
                    out += dispatch(&d);
                }
                return out == 35 ? 0 : 1;
            }
        """)
    )
    shim_object = tmp_path / "shim.o"
    main_object = tmp_path / "main.o"
    binary = tmp_path / "app"
    # The shim is C: a C++ driver would compile the .c file as C++ and fail, so
    # build it with the C compiler and link the instrumented C++ object with the
    # C++ driver. Only the C++ translation unit carries -finstrument-functions.
    subprocess.run(
        [str(cc), "-pthread", "-c", str(_SHIM), "-o", str(shim_object)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(cxx),
            "-finstrument-functions",
            "-g",
            "-O0",
            "-c",
            str(tmp_path / "main.cpp"),
            "-o",
            str(main_object),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [str(cxx), "-pthread", str(main_object), str(shim_object), "-o", str(binary)],
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
    header, records_iter = read_trace_file(output)
    assert header.language == cs.TRACE_LANGUAGE_CPP
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records_iter}
    # The virtual call a->speak() resolves to Dog::speak - the runtime-only edge
    # static analysis cannot resolve through the abstract Animal interface.
    virtual = edges.get(("dispatch", "speak"))
    assert virtual is not None, sorted(edges)
    assert virtual.count == 5


_CMAKE_LISTS = """\
cmake_minimum_required(VERSION 3.13)
project(cgrdemo C CXX)
find_package(Threads REQUIRED)
add_executable(app main.cpp reg.c cgr_trace_shim.c)
# The traced build type: -O0 keeps every frame (no inlining elides callees),
# -g emits the DWARF the offline symboliser reads. The shim self-excludes with
# no_instrument_function, so applying the flag to the whole target is safe.
target_compile_options(app PRIVATE -finstrument-functions -g -O0)
# The shim uses pthread_mutex_*/pthread_once; link pthreads explicitly so the
# build works on toolchains where they are separate from libc.
target_link_libraries(app PRIVATE Threads::Threads)
"""

_CMAKE_MAIN = """\
struct Animal {
    virtual ~Animal() = default;
    virtual int speak() = 0;
};
struct Dog : Animal { int speak() override; };
struct Cat : Animal { int speak() override; };
int Dog::speak() { return 7; }
int Cat::speak() { return 9; }

static int dispatch(Animal* a) { return a->speak(); }

// One template, two instantiations: apply<Dog> and apply<Cat> must collapse to
// the one source definition `apply`, not two `apply<...>` nodes.
template <typename T>
static int apply(T* t) { return t->speak() + 1; }

extern "C" int run_ptr();

int main() {
    Dog d;
    Cat c;
    int out = 0;
    for (int i = 0; i < 5; i++) out += dispatch(&d);
    for (int i = 0; i < 3; i++) out += dispatch(&c);
    for (int i = 0; i < 4; i++) {
        out += apply<Dog>(&d);
        out += apply<Cat>(&c);
    }
    out += run_ptr();
    return out > 0 ? 0 : 1;
}
"""

_CMAKE_REG = """\
static int greet(void) { return 1; }
static int (*fp)(void);
int run_ptr(void) {
    fp = greet;              /* call through a function pointer (C) */
    return fp();
}
"""


@pytest.mark.slow
@pytest.mark.skipif(
    sys.platform == "win32"
    or cmake is None
    or cc is None
    or cxx is None
    or atos is None,
    reason="cmake/C/C++ toolchain unavailable, or Windows/PE (shim targets ELF/Mach-O)",
)
def test_live_cmake_project_produces_dynamic_edges(tmp_path):
    # AC: a traced test run of a sample CMake project produces dynamic edges,
    # with a function-pointer edge (C) and a virtual edge (C++), and template
    # instantiations collapsed to their one source definition.
    (tmp_path / "CMakeLists.txt").write_text(_CMAKE_LISTS)
    (tmp_path / "main.cpp").write_text(_CMAKE_MAIN)
    (tmp_path / "reg.c").write_text(_CMAKE_REG)
    shutil.copy(_SHIM, tmp_path / "cgr_trace_shim.c")

    build = tmp_path / "build"
    subprocess.run(
        [str(cmake), "-S", str(tmp_path), "-B", str(build)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(cmake), "--build", str(build)],
        check=True,
        capture_output=True,
        text=True,
    )
    addrs = tmp_path / "cgr-trace.addrs"
    subprocess.run(
        [str(build / "app")],
        check=True,
        capture_output=True,
        env=dict(os.environ, CGR_TRACE_ADDRS=str(addrs)),
        cwd=tmp_path,
    )

    output = tmp_path / "trace.jsonl"
    count = convert_instrumented(addrs, repo_root=tmp_path, output=output)

    assert count > 0
    header, records_iter = read_trace_file(output)
    assert header.language == cs.TRACE_LANGUAGE_CPP
    records = list(records_iter)

    def edge_count(caller: str, callee: str) -> int:
        return sum(
            r.count
            for r in records
            if r.caller.qualname == caller and r.callee.qualname == callee
        )

    def callee_lines(caller: str, callee: str) -> set[int]:
        return {
            r.callee.line
            for r in records
            if r.caller.qualname == caller and r.callee.qualname == callee
        }

    edges = {(r.caller.qualname, r.callee.qualname) for r in records}

    # Virtual dispatch through the abstract Animal interface (C++ runtime-only):
    # both concrete receivers are recovered as distinct source positions.
    assert edge_count("dispatch", "speak") == 8  # 5 * Dog + 3 * Cat
    assert len(callee_lines("dispatch", "speak")) == 2, records

    # Function-pointer call resolved at runtime (C runtime-only).
    assert ("run_ptr", "greet") in edges, sorted(edges)

    # Both apply<Dog> and apply<Cat> collapse onto the one `apply` node, yet the
    # two concrete callees keep their own source positions.
    speak_callers = {caller for caller, callee in edges if callee == "speak"}
    assert speak_callers == {"dispatch", "apply"}, sorted(speak_callers)
    assert edge_count("apply", "speak") == 8  # 4 * Dog + 4 * Cat
    assert len(callee_lines("apply", "speak")) == 2, records

    # No template argument or return type leaks into any qualname; an operator
    # name legitimately carries spaces (operator new[], a conversion type) so it
    # is exempt from the no-space check.
    for record in records:
        for frame in (record.caller, record.callee):
            assert "<" not in frame.qualname, frame.qualname
            if not frame.qualname.startswith("operator"):
                assert " " not in frame.qualname, frame.qualname
