# A macro invocation followed by a block (`FMT_CATCH(...) {}`) or swept
# into member-init recovery (`FMT_APPLY_VARIADIC(expr);`) parses as a
# TYPE-LESS function_definition/declaration named after the macro. Valid
# C++ only omits the return type on a constructor, whose plain-identifier
# declarator repeats the enclosing class name; any other type-less
# plain-identifier definition is a parse artifact and must not mint a
# phantom Function/Method node (fmt's 5 FMT_CATCH + FMT_APPLY_VARIADIC
# dead-code false positives).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

MODULE_SCOPE_CATCH_CC = """
void format_system_error(int error_code, const char* message) noexcept {
  FMT_TRY {
    write(error_code, message);
    return;
  }
  FMT_CATCH(...) {}
  format_error_code(error_code, message);
}
"""

IN_CLASS_CATCH_H = """
template <typename T> struct formatter<T, char> {
  auto format(const T& value) -> int {
    FMT_TRY {
      return write(value);
    }
    FMT_CATCH(...) {}
    return 0;
  }
};
"""

IN_CLASS_VARIADIC_H = """
template <typename Context, int NUM_ARGS>
struct named_arg_store {
  int args[NUM_ARGS + 1u];
  int named_args[2];

  template <typename... T>
  FMT_CONSTEXPR FMT_ALWAYS_INLINE named_arg_store(T&... values)
      : args{{named_args, NUM_NAMED_ARGS}, values...} {
    int arg_index = 0, named_arg_index = 0;
    FMT_APPLY_VARIADIC(
        init_named_arg(named_args, arg_index, named_arg_index, values));
  }
};
"""

CTOR_CONTROL_CC = """
struct widget {
  widget(int x);
  int x_;
};
widget::widget(int x) : x_(x) {}
"""

# Simulates recovery-orphaned ctors: type-less plain-identifier definitions
# at module scope (fmt's os.h `file(int fd)` whose class ancestor the parse
# recovery destroyed). Named parameters prove `file(int fd)` real; the
# zero-param `file()` shares the macro shape and survives only because a
# registered class bears its name; UNKNOWN_MACRO() has neither and drops.
ORPHANED_CTOR_CC = """
struct file {
  int fd_;
};

file(int fd) { helper(fd); }
UNKNOWN_MACRO() {}
"""

ORPHANED_ZERO_PARAM_CTOR_CC = """
struct pipe {
  int fd_;
};

pipe() {}
"""

# Identifiers reachable only OUTSIDE the declarator-field path must not
# count as named parameters: `x` names the inner fn-ptr's parameter and
# `MAX_SIZE` is an array bound, so both callbacks stay macro artifacts.
# The reference-parameter ctor is the counter-control: `s` hangs off a
# reference_declarator as a bare child, not a `declarator` field, and must
# still register.
NESTED_IDENTIFIER_MACROS_CC = """
TRACE_CB(void (*)(int x)) {}
DECLARE_POOL(int[MAX_SIZE]) {}

view(const view& s) { helper(s); }
"""


def _def_qns(mock_ingestor: MagicMock) -> set[str]:
    labels = {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value}
    return {
        c.args[1].get("qualified_name")
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) in labels
    }


def test_module_scope_macro_call_block_not_registered(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "err.cc").write_text(MODULE_SCOPE_CATCH_CC)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    assert not any(qn.endswith(".FMT_CATCH") for qn in qns), qns
    # The mangled-but-real enclosing function must still register.
    assert any(qn.endswith(".format_system_error") for qn in qns), qns


def test_in_class_macro_call_block_not_registered(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "std.h").write_text(IN_CLASS_CATCH_H)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    assert not any(qn.endswith(".FMT_CATCH") for qn in qns), qns
    assert any(qn.endswith(".format") for qn in qns), qns


def test_in_class_macro_call_declaration_not_registered(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "base.h").write_text(IN_CLASS_VARIADIC_H)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    assert not any(qn.endswith(".FMT_APPLY_VARIADIC") for qn in qns), qns


def test_type_less_ctor_still_registered(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "widget.cc").write_text(CTOR_CONTROL_CC)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    # Both the in-class type-less ctor declaration and the out-of-class
    # qualified definition must survive the artifact guard.
    assert any(qn.endswith(".widget.widget") for qn in qns), qns


def test_orphaned_ctor_shapes_kept_and_macro_dropped(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "os.cc").write_text(ORPHANED_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    # The named parameter proves `file(int fd)` is a real (orphaned) ctor.
    assert any(qn.endswith(".file") or ".file@" in qn for qn in qns), qns
    assert not any("UNKNOWN_MACRO" in qn for qn in qns), qns


def test_orphaned_zero_param_ctor_kept_via_class_registry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "pipe.cc").write_text(ORPHANED_ZERO_PARAM_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    # `pipe() {}` shares the macro-invocation shape; the registered class
    # `pipe` is what keeps it alive.
    assert any(qn.endswith(".pipe") or ".pipe@" in qn for qn in qns), qns


def test_nested_identifiers_do_not_count_as_named_params(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "cb.cc").write_text(NESTED_IDENTIFIER_MACROS_CC)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    assert not any("TRACE_CB" in qn for qn in qns), qns
    assert not any("DECLARE_POOL" in qn for qn in qns), qns
    assert any(qn.endswith(".view") or ".view@" in qn for qn in qns), qns


PIPE_HDR = """
#pragma once
struct pipe {
  int fd_;
};
"""

PIPE_CC = """
#include "pipe.h"

pipe() {}
"""


def test_incremental_reparse_keeps_orphan_ctor_from_unchanged_header(
    temp_repo: Path,
) -> None:
    # Incremental runs rehydrate the registry from the graph AFTER the
    # per-file passes; the artifact tiebreak must wait for rehydration or a
    # re-parsed file's orphaned ctor whose class lives in an UNCHANGED
    # header is dropped as a macro artifact (PR #788 review finding).
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from evals.cgr_graph import _StatefulIngestor

    (temp_repo / "pipe.h").write_text(PIPE_HDR)
    (temp_repo / "pipe.cc").write_text(PIPE_CC)

    def index(store: _StatefulIngestor, force: bool) -> None:
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
        ).run(force=force)

    def ctor_nodes(store: _StatefulIngestor) -> set[str]:
        # The kept orphan ctor registers as a METHOD reattached under the
        # rehydrated class (`...pipe.pipe`), no longer a module Function.
        return {
            str(qn)
            for (label, qn) in store.nodes
            if label == cs.NodeLabel.METHOD.value and str(qn).endswith(".pipe.pipe")
        }

    store = _StatefulIngestor()
    index(store, force=True)
    assert ctor_nodes(store), sorted(store.nodes)

    # A trailing comment changes the hash but not the AST, so only
    # pipe.cc re-parses; the class comes from rehydration alone.
    (temp_repo / "pipe.cc").write_text(PIPE_CC + "// touched\n")
    index(store, force=False)
    assert ctor_nodes(store), sorted(store.nodes)
