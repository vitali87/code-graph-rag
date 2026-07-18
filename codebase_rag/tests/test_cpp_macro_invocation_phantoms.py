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
