from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.cpp_frontend import cpp_frontend_available, run_cpp_frontend

pytestmark = pytest.mark.skipif(
    not cpp_frontend_available(),
    reason="libclang not available",
)

# Preprocessor macros were invisible to the frontend (no
# PARSE_DETAILED_PROCESSING_RECORD): SQUARE(v) had no node and no CALLS edge.
# Macros register as Function nodes (the cross-language decision);
# MACRO_INSTANTIATION.referenced gives the definition, and the caller is the
# tightest enclosing function/method span (macro cursors are TU-level, so
# enclosure is recovered by span). Empty-bodied object-like macros (include
# guards, feature flags) are NOT nodes, nor are compiler or system macros.
_CALC_H = """\
#ifndef CALC_H
#define CALC_H
#include <limits.h>
#define SQUARE(x) ((x)*(x))
#define MAX_SIZE 100
int compute(int v);
#endif
"""

_SRC = """\
#include "calc.h"
int compute(int v) { return SQUARE(v) + MAX_SIZE + CMDLINE_LIMIT + INT_MAX; }
int main() { return compute(2); }
"""


def _write(root: Path) -> None:
    root.mkdir()
    (root / "calc.h").write_text(_CALC_H, encoding="utf-8")
    (root / "calc.cpp").write_text(_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": [
                        "c++",
                        "-std=c++17",
                        "-DCMDLINE_LIMIT=7",
                        f"-I{root}",
                        str(root / "calc.cpp"),
                    ],
                    "file": str(root / "calc.cpp"),
                }
            ]
        ),
        encoding="utf-8",
    )


def _functions(ingestor: MagicMock) -> set[str]:
    return {
        c.args[1]["qualified_name"]
        for c in ingestor.ensure_node_batch.call_args_list
        if c.args[0] == "Function"
    }


def _calls(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    }


def test_macro_definitions_register_as_functions(temp_repo: Path) -> None:
    root = temp_repo / "macproj"
    _write(root)
    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)
    functions = _functions(ingestor)
    # calc.cpp claims macproj.calc (walk order), so calc.h keeps its ext
    assert "macproj.calc.h.SQUARE" in functions, sorted(functions)
    assert "macproj.calc.h.MAX_SIZE" in functions, sorted(functions)
    # the include guard is an empty flag, not a callable
    assert not any(qn.endswith(".CALC_H") for qn in functions), sorted(functions)
    # builtins/system macros live outside the repo; a command-line -D macro
    # has no file at all, so none are nodes and their use sites carry no edges
    assert not any("__GNUC__" in qn for qn in functions), sorted(functions)
    assert not any("CMDLINE_LIMIT" in qn for qn in functions), sorted(functions)
    assert not any("INT_MAX" in qn for qn in functions), sorted(functions)


def test_macro_instantiations_emit_calls_from_enclosing_function(
    temp_repo: Path,
) -> None:
    root = temp_repo / "macproj2"
    _write(root)
    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)
    calls = _calls(ingestor)
    assert ("macproj2.calc.compute", "macproj2.calc.h.SQUARE") in calls, sorted(calls)
    assert ("macproj2.calc.compute", "macproj2.calc.h.MAX_SIZE") in calls, sorted(calls)


def test_file_scope_macro_use_attributes_to_module(temp_repo: Path) -> None:
    # A macro expanded outside any function span (a file-scope global
    # initializer) attributes its CALLS to the Module, mirroring the
    # module-caller rule for ordinary calls.
    root = temp_repo / "macmod"
    root.mkdir()
    (root / "calc.h").write_text(_CALC_H, encoding="utf-8")
    (root / "calc.cpp").write_text(
        '#include "calc.h"\nint global_limit = MAX_SIZE;\n', encoding="utf-8"
    )
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": [
                        "c++",
                        "-std=c++17",
                        f"-I{root}",
                        str(root / "calc.cpp"),
                    ],
                    "file": str(root / "calc.cpp"),
                }
            ]
        ),
        encoding="utf-8",
    )
    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)
    calls = _calls(ingestor)
    assert ("macmod.calc", "macmod.calc.h.MAX_SIZE") in calls, sorted(calls)


def test_macro_use_outside_repo_emits_no_edge(temp_repo: Path) -> None:
    # A TU outside the indexed repo (rel unresolvable) and a TU in an
    # ignored dir (rel resolves but has no module qn, e.g. build/) both use
    # a repo macro: the definition node still registers via the header, but
    # neither use site can carry a CALLS edge.
    root = temp_repo / "macext"
    root.mkdir()
    (root / "calc.h").write_text(_CALC_H, encoding="utf-8")
    outside = temp_repo / "outside_main.cpp"
    outside.write_text(
        '#include "calc.h"\nint compute(int v) { return SQUARE(v); }\n',
        encoding="utf-8",
    )
    build_dir = root / "build"
    build_dir.mkdir()
    (build_dir / "gen.cpp").write_text(
        # a macro DEFINED in the ignored dir: rel resolves but there is no
        # module qn, so it must not become a node either
        '#include "calc.h"\n#define GEN_LIMIT 42\n'
        "int generated_limit = MAX_SIZE + GEN_LIMIT;\n",
        encoding="utf-8",
    )
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": ["c++", "-std=c++17", f"-I{root}", str(outside)],
                    "file": str(outside),
                },
                {
                    "directory": str(root),
                    "arguments": [
                        "c++",
                        "-std=c++17",
                        f"-I{root}",
                        str(build_dir / "gen.cpp"),
                    ],
                    "file": str(build_dir / "gen.cpp"),
                },
            ]
        ),
        encoding="utf-8",
    )
    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)
    # no calc.cpp in this fixture, so calc.h claims the plain module qn
    assert "macext.calc.SQUARE" in _functions(ingestor)
    assert not any("GEN_LIMIT" in qn for qn in _functions(ingestor))
    assert not any("SQUARE" in t or "MAX_SIZE" in t for _, t in _calls(ingestor)), (
        sorted(_calls(ingestor))
    )
