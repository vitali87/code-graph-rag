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

# (H) Preprocessor macros were invisible to the frontend (no
# (H) PARSE_DETAILED_PROCESSING_RECORD): SQUARE(v) had no node and no CALLS
# (H) edge. Macros register as Function nodes (the cross-language decision);
# (H) MACRO_INSTANTIATION.referenced gives the exact definition, and the caller
# (H) is the tightest enclosing function/method span (macro cursors are TU-level
# (H) preprocessing entities, so lexical enclosure must be recovered by span).
# (H) Empty-bodied object-like macros (include guards, feature flags) are NOT
# (H) nodes; neither are compiler builtins or system-header macros.
_CALC_H = """\
#ifndef CALC_H
#define CALC_H
#define SQUARE(x) ((x)*(x))
#define MAX_SIZE 100
int compute(int v);
#endif
"""

_SRC = """\
#include "calc.h"
int compute(int v) { return SQUARE(v) + MAX_SIZE; }
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
    # (H) calc.cpp claims macproj.calc (walk order), so calc.h keeps its ext
    assert "macproj.calc.h.SQUARE" in functions, sorted(functions)
    assert "macproj.calc.h.MAX_SIZE" in functions, sorted(functions)
    # (H) the include guard is an empty flag, not a callable
    assert not any(qn.endswith(".CALC_H") for qn in functions), sorted(functions)
    # (H) builtins/system macros live outside the repo
    assert not any("__GNUC__" in qn for qn in functions), sorted(functions)


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
