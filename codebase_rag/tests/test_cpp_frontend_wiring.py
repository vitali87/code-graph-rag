from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.cpp_frontend import cpp_frontend_available
from codebase_rag.tests.conftest import get_nodes, get_qualified_names, run_updater

pytestmark = pytest.mark.skipif(
    not cpp_frontend_available(),
    reason="libclang not available",
)

# (H) `struct WIDGET_API Widget` is a macro tree-sitter cannot expand: it loses
# (H) the Widget class. The libclang frontend recovers it. The wiring decides
# (H) which path runs, gated on CPP_FRONTEND + a discoverable compile_commands.
_HEADER = """
#define WIDGET_API

namespace ui {

struct WIDGET_API Widget {
    int handle;
    void show();
};

}  // namespace ui
"""

_SRC = """
#include "widget.h"
namespace ui {
void Widget::show() {}
}
"""


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "widget.h").write_text(_HEADER, encoding="utf-8")
    (root / "widget.cpp").write_text(_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": ["c++", "-std=c++17", str(root / "widget.cpp")],
                    "file": str(root / "widget.cpp"),
                }
            ]
        ),
        encoding="utf-8",
    )


def test_default_treesitter_does_not_recover_macro_class(temp_repo: Path) -> None:
    root = temp_repo / "defaultproj"
    _write_project(root)

    ingestor = MagicMock()
    run_updater(root, ingestor)
    classes = get_qualified_names(get_nodes(ingestor, "Class"))

    # (H) No regression: with the default flag, indexing is the tree-sitter path,
    # (H) which mis-parses the macro and never produces ui.Widget.
    assert not any(q.endswith(".ui.Widget") for q in classes), (
        f"default path should not engage the frontend: {classes}"
    )


def test_libclang_frontend_recovers_macro_class(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "libclangproj"
    _write_project(root)

    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)

    ingestor = MagicMock()
    run_updater(root, ingestor)

    classes = get_qualified_names(get_nodes(ingestor, "Class"))
    methods = get_qualified_names(get_nodes(ingestor, "Method"))

    # (H) The frontend recovers the real class and binds the out-of-line method.
    assert any(q.endswith(".ui.Widget") for q in classes), (
        f"frontend did not recover Widget: {classes}"
    )
    assert any(q.endswith(".ui.Widget.show") for q in methods), (
        f"frontend did not bind Widget::show: {methods}"
    )
    # (H) The covered file was NOT also processed by tree-sitter (no double-parse
    # (H) producing the macro-mangled class).
    assert not any(q.endswith(".ui.WIDGET_API") for q in classes), (
        f"tree-sitter should have skipped the covered file: {classes}"
    )
