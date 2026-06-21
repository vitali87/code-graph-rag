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

# (H) An out-of-line method calling a free function. tree-sitter's cgr path
# (H) historically dangled the caller qn (PR #47); libclang resolves the call
# (H) target via cursor.referenced with no name heuristic, and the frontend
# (H) anchors the caller to the method node itself.
_HEADER = """
namespace m {

class Calc {
public:
    int add(int a, int b);
};

int helper(int x);

}  // namespace m
"""

_SRC = """
#include "calc.h"
namespace m {
int helper(int x) { return x + 1; }
int Calc::add(int a, int b) { return helper(a) + b; }
}
"""


def _write(root: Path) -> None:
    root.mkdir()
    (root / "calc.h").write_text(_HEADER, encoding="utf-8")
    (root / "calc.cpp").write_text(_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": ["c++", "-std=c++17", str(root / "calc.cpp")],
                    "file": str(root / "calc.cpp"),
                }
            ]
        ),
        encoding="utf-8",
    )


def _calls(ingestor: MagicMock) -> list[tuple[str, str, str, str]]:
    out = []
    for c in ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] == "CALLS":
            (from_label, _, from_qn) = c.args[0]
            (to_label, _, to_qn) = c.args[2]
            out.append((from_label, from_qn, to_label, to_qn))
    return out


def test_method_calls_free_function(temp_repo: Path) -> None:
    root = temp_repo / "callsproj"
    _write(root)

    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)

    calls = _calls(ingestor)
    # (H) The caller is the METHOD node (not a dangling free-function/module qn).
    assert any(
        from_label == "Method"
        and from_qn.endswith(".m.Calc.add")
        and to_label == "Function"
        and to_qn.endswith(".m.helper")
        for from_label, from_qn, to_label, to_qn in calls
    ), f"expected Calc.add CALLS helper, got {calls}"
