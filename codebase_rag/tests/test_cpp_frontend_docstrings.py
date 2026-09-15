"""The pure-libclang C++ frontend carries Doxygen comments onto its nodes.

Greptile on PR #1888: `_node_props` hard-coded `docstring=None`, so with
`CPP_FRONTEND=libclang` every documented class, function, method and alias
lost its documentation while the tree-sitter path (the hybrid default) kept
it. libclang attaches a comment to the cursor it documents (`raw_comment`);
the frontend now cleans and stores it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.cpp_frontend import cpp_frontend_available, run_cpp_frontend
from codebase_rag.tests.conftest import get_nodes

pytestmark = pytest.mark.skipif(
    not cpp_frontend_available(),
    reason="libclang not available",
)

_HEADER = """
namespace n {
/** A documented box. */
class Box {
public:
    /// Opens the box.
    /// Second line.
    void open();
    void undocumented();
};

/*! Frees a box. */
void release(Box* b);

int plain(int x);
}  // namespace n
"""
_SRC = '#include "docs.h"\nvoid n::Box::open() {}\nvoid n::Box::undocumented() {}\nvoid n::release(Box*) {}\nint n::plain(int x) { return x; }\n'


def _write(root: Path) -> None:
    root.mkdir()
    (root / "docs.h").write_text(_HEADER, encoding="utf-8")
    (root / "docs.cpp").write_text(_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": ["c++", "-std=c++17", str(root / "docs.cpp")],
                    "file": str(root / "docs.cpp"),
                }
            ]
        ),
        encoding="utf-8",
    )


def _docstrings(ingestor: MagicMock, label: str) -> dict[str, str | None]:
    """{name: docstring} for every emitted node of `label`.

    `get_nodes` returns the `ensure_node_batch` call objects; the properties
    dict is the second positional argument, and `_node_props` always sets
    `name`.
    """
    return {
        call[0][1]["name"]: call[0][1].get("docstring")
        for call in get_nodes(ingestor, label)
    }


def test_documented_definitions_carry_their_doxygen_comment(temp_repo: Path) -> None:
    root = temp_repo / "docsproj"
    _write(root)
    ingestor = MagicMock()
    run_cpp_frontend(ingestor, root, root.name, root)

    classes = _docstrings(ingestor, "Class")
    assert classes.get("Box") == "A documented box.", classes

    methods = _docstrings(ingestor, "Method")
    assert methods.get("open") == "Opens the box.\nSecond line.", methods
    # The known-negative: an undocumented member stays None, not "" or a
    # neighbour's comment.
    assert "undocumented" in methods, methods
    assert methods["undocumented"] is None, methods

    functions = _docstrings(ingestor, "Function")
    assert functions.get("release") == "Frees a box.", functions
    assert "plain" in functions, functions
    assert functions["plain"] is None, functions
