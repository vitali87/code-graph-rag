from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp_frontend import (
    cpp_frontend_available,
    run_cpp_frontend_hybrid,
)
from codebase_rag.tests.conftest import get_nodes, get_qualified_names, run_updater
from evals.cgr_graph import _StatefulIngestor

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

# (H) QUAD expands SQUARE only when QUAD itself expands -- a NESTED expansion,
# (H) which the preprocessing record does not report as a MACRO_INSTANTIATION.
# (H) The definition-body reference is the only evidence SQUARE is used, so it
# (H) must carry a macro -> macro CALLS edge or SQUARE reports dead.
_NESTED_H = """\
#ifndef NESTED_H
#define NESTED_H
#define SQUARE(x) ((x)*(x))
#define QUAD(x) (SQUARE(x)*SQUARE(x))
int compute(int v);
#endif
"""

_NESTED_SRC = """\
#include "nested.h"
int compute(int v) { return QUAD(v); }
"""


def _compdb_entry(root: Path, source: Path) -> dict[str, str | list[str]]:
    return {
        "directory": str(root),
        "arguments": ["c++", "-std=c++17", f"-I{root}", str(source)],
        "file": str(source),
    }


def _write_nested(root: Path) -> None:
    root.mkdir()
    (root / "nested.h").write_text(_NESTED_H, encoding="utf-8")
    (root / "nested.cpp").write_text(_NESTED_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps([_compdb_entry(root, root / "nested.cpp")]), encoding="utf-8"
    )


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


# (H) WRAP's parameter shadows the SQUARE macro: the body's SQUARE token is
# (H) substituted by the caller's argument, never an expansion of the macro,
# (H) so WRAP -> SQUARE must not exist. ALIAS is the positive control: an
# (H) object-like macro whose parenthesized body genuinely references SQUARE.
_PARAM_H = """\
#ifndef PARAM_H
#define PARAM_H
#define SQUARE(x) ((x)*(x))
#define WRAP(SQUARE) (SQUARE + 1)
#define ALIAS (SQUARE(2))
int compute(int v);
#endif
"""

_PARAM_SRC = """\
#include "param.h"
int compute(int v) { return WRAP(v) + ALIAS; }
"""


def _write_param(root: Path) -> None:
    root.mkdir()
    (root / "param.h").write_text(_PARAM_H, encoding="utf-8")
    (root / "param.cpp").write_text(_PARAM_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps([_compdb_entry(root, root / "param.cpp")]), encoding="utf-8"
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
    # (H) no ns.cpp in this fixture, so ns.h claims the plain module qn
    assert "hybns.ns.NS_BEGIN" in functions, sorted(functions)


def test_hybrid_macro_body_reference_emits_macro_to_macro_call(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybnest"
    _write_nested(root)
    ingestor = _run_hybrid(root, monkeypatch)
    calls = _calls(ingestor)
    assert ("hybnest.nested.h.QUAD", "hybnest.nested.h.SQUARE") in calls, sorted(calls)
    # (H) a macro does not call itself
    assert ("hybnest.nested.h.SQUARE", "hybnest.nested.h.SQUARE") not in calls


def test_hybrid_macro_parameter_shadowing_a_macro_is_not_a_call(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybparam"
    _write_param(root)
    ingestor = _run_hybrid(root, monkeypatch)
    calls = _calls(ingestor)
    assert ("hybparam.param.h.WRAP", "hybparam.param.h.SQUARE") not in calls, sorted(
        calls
    )
    assert ("hybparam.param.h.ALIAS", "hybparam.param.h.SQUARE") in calls, sorted(calls)


def test_hybrid_incremental_run_keeps_macro_callers_for_unchanged_files(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybinc"
    _write_calc(root)
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    gu.GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    ).run(force=True)

    # (H) A new unrelated file makes the second run incremental-but-dirty
    # (H) while calc.cpp itself stays unchanged, so Pass 2 records no spans
    # (H) for it; the frontend still queues its macro uses (libclang parses
    # (H) every TU each run).
    (root / "other.cpp").write_text("int other_fn() { return 0; }\n", encoding="utf-8")
    gu.GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    ).run(force=False)

    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel_type, _tl, to_val in store.edges
        if rel_type == cs.RelationshipType.CALLS.value
    }
    # (H) SQUARE's only use is inside compute(); span-less resolution on the
    # (H) incremental run would wrongly re-attribute it to the Module
    assert ("hybinc.calc", "hybinc.calc.h.SQUARE") not in calls, sorted(calls)
    assert ("hybinc.calc.compute", "hybinc.calc.h.SQUARE") in calls, sorted(calls)


def test_hybrid_incremental_header_edit_keeps_macro_nodes(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybedit"
    _write_calc(root)
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    gu.GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    ).run(force=True)
    macro_node = (cs.NodeLabel.FUNCTION.value, "hybedit.calc.h.SQUARE")
    assert macro_node in store.nodes

    # (H) Editing the header makes it a changed file: Pass 2 deletes its
    # (H) Module subtree before re-parsing, so macro nodes emitted BEFORE the
    # (H) delete would vanish until a forced rebuild -- the frontend must run
    # (H) after the deletes.
    (root / "calc.h").write_text(_CALC_H + "// touched\n", encoding="utf-8")
    gu.GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    ).run(force=False)
    assert macro_node in store.nodes, sorted(
        str(uid) for label, uid in store.nodes if label == cs.NodeLabel.FUNCTION.value
    )


def test_hybrid_drops_macro_uses_in_ignored_directories(temp_repo: Path) -> None:
    root = temp_repo / "hybskip"
    _write_calc(root)
    (root / "build").mkdir()
    (root / "build" / "gen.h").write_text(
        '#include "calc.h"\nstatic int cap = MAX_SIZE;\n', encoding="utf-8"
    )
    (root / "calc.cpp").write_text(
        '#include "build/gen.h"\n' + _CALC_SRC, encoding="utf-8"
    )
    pending = run_cpp_frontend_hybrid(MagicMock(), root, root.name, root)
    # (H) build/ is an ignored directory: its files carry no module qn, so a
    # (H) macro use there has no possible Module fallback and must be dropped
    assert all(p.rel_path != "build/gen.h" for p in pending), pending
    assert any(p.rel_path == "calc.cpp" for p in pending), pending


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


# (H) A config macro consumed ONLY by preprocessor conditionals (fmt's
# (H) FMT_USE_FULL_CACHE_DRAGONBOX shape): libclang reports a
# (H) MACRO_INSTANTIATION for every EVALUATED directive condition (#if X,
# (H) #ifdef X, #if defined(X), a reached #elif), and the use sits outside
# (H) every definition span, so it must attribute to the Module and keep the
# (H) macro reachable. A condition in a SKIPPED branch is never evaluated and
# (H) carries no edge -- the graph mirrors the build configuration.
_FLAGS_SRC = """\
#define USE_CACHE 1
#define HAS_MODE 2
#define SKIPPED_FLAG 3
int base = 1;
#if USE_CACHE
int cache = 1;
#endif
#ifdef HAS_MODE
int mode = 1;
#endif
#if 0
#if SKIPPED_FLAG
int never = 1;
#endif
#endif
int main() { return base; }
"""


def _write_flags(root: Path) -> None:
    root.mkdir()
    (root / "conf.cpp").write_text(_FLAGS_SRC, encoding="utf-8")
    (root / "compile_commands.json").write_text(
        json.dumps([_compdb_entry(root, root / "conf.cpp")]), encoding="utf-8"
    )


def test_hybrid_directive_only_macro_use_attributes_to_module(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "hybdirective"
    _write_flags(root)
    ingestor = _run_hybrid(root, monkeypatch)
    calls = _calls(ingestor)
    assert ("hybdirective.conf", "hybdirective.conf.USE_CACHE") in calls, sorted(calls)
    assert ("hybdirective.conf", "hybdirective.conf.HAS_MODE") in calls, sorted(calls)
    # (H) a condition inside a SKIPPED branch (#if 0) is never evaluated, so
    # (H) it is never tokenized and carries no edge -- the graph mirrors the
    # (H) build configuration
    assert not any(t.endswith(".SKIPPED_FLAG") for _, t in calls), sorted(calls)
