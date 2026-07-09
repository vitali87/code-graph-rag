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

# (H) `#include "calc.h"` is the C++ import: the frontend must emit an IMPORTS
# (H) edge Module -> Module for every within-repo inclusion (transitively --
# (H) calc.h including util.h counts, from calc.h). System headers (<vector>)
# (H) resolve outside the repo and emit nothing, and a file never imports itself
# (H) (the tree-sitter path's self-import bug must not be replicated).
_UTIL_H = """
#pragma once
namespace m { int util(int x); }
"""

_CALC_H = """
#pragma once
#include "util.h"
namespace m { int add(int a, int b); }
"""

_SRC = """
#include "calc.h"
#include <vector>
namespace m {
int add(int a, int b) { (void)std::vector<int>{}; return a + b + util(a); }
int util(int x) { return x; }
}
"""


def _write(root: Path) -> None:
    root.mkdir()
    (root / "util.h").write_text(_UTIL_H, encoding="utf-8")
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


def _imports(ingestor: MagicMock) -> set[tuple[str, str, str, str]]:
    out = set()
    for c in ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] == "IMPORTS":
            (from_label, _, from_qn) = c.args[0]
            (to_label, _, to_qn) = c.args[2]
            out.add((from_label, from_qn, to_label, to_qn))
    return out


def test_within_repo_includes_emit_imports(temp_repo: Path) -> None:
    root = temp_repo / "incproj"
    _write(root)

    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)

    imports = _imports(ingestor)
    # (H) The resolver's module qns: calc.cpp -> incproj.calc (extension
    # (H) stripped), calc.h -> incproj.calc.h (extension kept on stem
    # (H) collision), util.h -> incproj.util.
    assert ("Module", "incproj.calc", "Module", "incproj.calc.h") in imports, sorted(
        imports
    )
    assert any(
        from_qn == "incproj.calc.h" and to_qn == "incproj.util"
        for _, from_qn, _, to_qn in imports
    ), f"expected calc.h IMPORTS util.h, got {sorted(imports)}"
    # (H) no self-imports, no system-header edges
    assert not any(f == t for _, f, _, t in imports), sorted(imports)
    assert not any("vector" in t for _, _, _, t in imports), sorted(imports)
