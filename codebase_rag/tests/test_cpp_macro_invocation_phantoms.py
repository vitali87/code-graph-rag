# (H) A macro invocation followed by a block (`FMT_CATCH(...) {}`) or swept
# (H) into member-init recovery (`FMT_APPLY_VARIADIC(expr);`) parses as a
# (H) TYPE-LESS function_definition/declaration named after the macro. Valid
# (H) C++ only omits the return type on a constructor, whose plain-identifier
# (H) declarator repeats the enclosing class name; any other type-less
# (H) plain-identifier definition is a parse artifact and must not mint a
# (H) phantom Function/Method node (fmt's 5 FMT_CATCH + FMT_APPLY_VARIADIC
# (H) dead-code false positives).
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

# (H) Simulates recovery-orphaned ctors: type-less plain-identifier definitions
# (H) at module scope (fmt's os.h `file(int fd)` whose class ancestor the parse
# (H) recovery destroyed). Named parameters prove `file(int fd)` real; the
# (H) zero-param `file()` shares the macro shape and survives only because a
# (H) registered class bears its name; UNKNOWN_MACRO() has neither and drops.
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

# (H) Identifiers reachable only OUTSIDE the declarator-field path must not
# (H) count as named parameters: `x` names the inner fn-ptr's parameter and
# (H) `MAX_SIZE` is an array bound, so both callbacks stay macro artifacts.
# (H) The reference-parameter ctor is the counter-control: `s` hangs off a
# (H) reference_declarator as a bare child, not a `declarator` field, and must
# (H) still register.
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
    # (H) The mangled-but-real enclosing function must still register.
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
    # (H) Both the in-class type-less ctor declaration and the out-of-class
    # (H) qualified definition must survive the artifact guard.
    assert any(qn.endswith(".widget.widget") for qn in qns), qns


def test_orphaned_ctor_shapes_kept_and_macro_dropped(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "os.cc").write_text(ORPHANED_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    # (H) The named parameter proves `file(int fd)` is a real (orphaned) ctor.
    assert any(qn.endswith(".file") or ".file@" in qn for qn in qns), qns
    assert not any("UNKNOWN_MACRO" in qn for qn in qns), qns


def test_orphaned_zero_param_ctor_kept_via_class_registry(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "pipe.cc").write_text(ORPHANED_ZERO_PARAM_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    qns = _def_qns(mock_ingestor)
    # (H) `pipe() {}` shares the macro-invocation shape; the registered class
    # (H) `pipe` is what keeps it alive.
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
    # (H) Incremental runs rehydrate the registry from the graph AFTER the
    # (H) per-file passes; the artifact tiebreak must wait for rehydration or a
    # (H) re-parsed file's orphaned ctor whose class lives in an UNCHANGED
    # (H) header is dropped as a macro artifact (PR #788 review finding).
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
        return {
            str(qn)
            for (label, qn) in store.nodes
            if label == cs.NodeLabel.FUNCTION.value
            and (str(qn).endswith(".pipe") or ".pipe@" in str(qn))
        }

    store = _StatefulIngestor()
    index(store, force=True)
    assert ctor_nodes(store), sorted(store.nodes)

    # (H) A trailing comment changes the hash but not the AST, so only
    # (H) pipe.cc re-parses; the class comes from rehydration alone.
    (temp_repo / "pipe.cc").write_text(PIPE_CC + "// touched\n")
    index(store, force=False)
    assert ctor_nodes(store), sorted(store.nodes)
