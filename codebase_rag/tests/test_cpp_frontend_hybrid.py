from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.cpp_frontend import (
    cpp_frontend_available,
    run_cpp_frontend_hybrid,
)
from codebase_rag.tests.conftest import get_nodes, get_qualified_names, run_updater

pytestmark = pytest.mark.skipif(
    not cpp_frontend_available(),
    reason="libclang not available",
)

# (H) HYBRID mode: tree-sitter stays the backbone (every file gets its
# (H) tree-sitter definitions and CALLS; nothing is skipped) and libclang
# (H) layers on only the facts it is uniquely right about -- macro Function
# (H) nodes and #include IMPORTS. libclang/tree-sitter definition qns diverge
# (H) wherever macros hide namespaces, so the frontend must emit NO definition
# (H) nodes of its own; macro-use CALLS attribute to the tightest enclosing
# (H) TREE-SITTER definition span after Pass 2 (only module-level qns --
# (H) macros, Modules -- are scheme-identical and safe to emit directly).
_CALC_H = """\
#ifndef CALC_H
#define CALC_H
#define SQUARE(x) ((x)*(x))
#define MAX_SIZE 100
int compute(int v);
#endif
"""

# (H) MAX_SIZE is object-like: tree-sitter sees a bare identifier (not a call
# (H) expression), so the CALLS edge can only come from the hybrid span
# (H) resolution -- the test cannot pass via tree-sitter call binding.
_CALC_SRC = """\
#include "calc.h"
int compute(int v) {
    return MAX_SIZE + SQUARE(v);
}
int global_limit = MAX_SIZE;
"""


def _compdb_entry(root: Path, source: Path) -> dict[str, str | list[str]]:
    return {
        "directory": str(root),
        "arguments": ["c++", "-std=c++17", f"-I{root}", str(source)],
        "file": str(source),
    }


def _write_calc(root: Path) -> None:
    root.mkdir()
    (root / "calc.h").write_text(_CALC_H, encoding="utf-8")
    (root / "calc.cpp").write_text(_CALC_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps([_compdb_entry(root, root / "calc.cpp")]), encoding="utf-8"
    )


# (H) A macro-hidden namespace is exactly where the two qn schemes diverge:
# (H) libclang would emit widget.ui.helper, tree-sitter emits widget.helper.
_NS_H = """\
#ifndef NS_H
#define NS_H
#define NS_BEGIN namespace ui {
#define NS_END }
#endif
"""

_WIDGET_SRC = """\
#include "ns.h"
NS_BEGIN
int helper(int v) { return v + 1; }
NS_END
"""


def _write_widget(root: Path) -> None:
    root.mkdir()
    (root / "ns.h").write_text(_NS_H, encoding="utf-8")
    (root / "widget.cpp").write_text(_WIDGET_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps([_compdb_entry(root, root / "widget.cpp")]), encoding="utf-8"
    )


def _calls(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    }


def _imports(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "IMPORTS"
    }


def _run_hybrid(root: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    ingestor = MagicMock()
    run_updater(root, ingestor)
    return ingestor


def test_hybrid_macro_nodes_coexist_with_treesitter_definitions(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybproj"
    _write_calc(root)
    ingestor = _run_hybrid(root, monkeypatch)
    functions = get_qualified_names(get_nodes(ingestor, "Function"))
    # (H) frontend macro nodes AND the tree-sitter definition for the SAME
    # (H) file: covered files are not skipped in hybrid mode
    assert "hybproj.calc.h.SQUARE" in functions, sorted(functions)
    assert "hybproj.calc.h.MAX_SIZE" in functions, sorted(functions)
    assert "hybproj.calc.compute" in functions, sorted(functions)


def test_hybrid_macro_use_calls_from_treesitter_enclosing_function(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybcalls"
    _write_calc(root)
    ingestor = _run_hybrid(root, monkeypatch)
    calls = _calls(ingestor)
    assert ("hybcalls.calc.compute", "hybcalls.calc.h.MAX_SIZE") in calls, sorted(calls)
    assert ("hybcalls.calc.compute", "hybcalls.calc.h.SQUARE") in calls, sorted(calls)


def test_hybrid_file_scope_macro_use_attributes_to_module(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybmod"
    _write_calc(root)
    ingestor = _run_hybrid(root, monkeypatch)
    # (H) `int global_limit = MAX_SIZE;` expands outside every tree-sitter
    # (H) definition span -> the Module, mirroring the module-caller rule
    assert ("hybmod.calc", "hybmod.calc.h.MAX_SIZE") in _calls(ingestor), sorted(
        _calls(ingestor)
    )


def test_hybrid_emits_include_imports(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybimp"
    _write_calc(root)
    ingestor = _run_hybrid(root, monkeypatch)
    assert ("hybimp.calc", "hybimp.calc.h") in _imports(ingestor), sorted(
        _imports(ingestor)
    )


def test_hybrid_emits_no_libclang_scheme_definitions(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybns"
    _write_widget(root)
    ingestor = _run_hybrid(root, monkeypatch)
    functions = get_qualified_names(get_nodes(ingestor, "Function"))
    methods = get_qualified_names(get_nodes(ingestor, "Method"))
    # (H) the libclang scheme sees through NS_BEGIN and would emit
    # (H) widget.ui.helper -- a duplicate of tree-sitter's widget.helper under
    # (H) a qn no tree-sitter edge can ever reach
    assert not any(q.endswith(".ui.helper") for q in functions | methods), sorted(
        functions | methods
    )
    assert "hybns.ns.h.NS_BEGIN" in functions, sorted(functions)


def test_run_hybrid_emits_only_macros_and_returns_pending_calls(
    temp_repo: Path,
) -> None:
    root = temp_repo / "hybunit"
    _write_calc(root)
    ingestor = MagicMock()
    pending = run_cpp_frontend_hybrid(ingestor, root, root.name, root)
    # (H) macros only: no definition nodes, no CALLS (callers are unknowable
    # (H) until the tree-sitter pass has run), includes still emitted
    functions = get_qualified_names(get_nodes(ingestor, "Function"))
    assert "hybunit.calc.h.SQUARE" in functions, sorted(functions)
    assert "hybunit.calc.compute" not in functions, sorted(functions)
    assert not get_nodes(ingestor, "Class")
    assert not get_nodes(ingestor, "Method")
    assert not _calls(ingestor)
    assert ("hybunit.calc", "hybunit.calc.h") in _imports(ingestor)
    pending_callees = {p.callee_qn for p in pending}
    assert "hybunit.calc.h.SQUARE" in pending_callees, pending
    assert "hybunit.calc.h.MAX_SIZE" in pending_callees, pending
    assert all(p.rel_path == "calc.cpp" for p in pending), pending
