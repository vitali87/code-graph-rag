# (H) A ctor's member initializer list runs base-class ctors (`: buffer(g, 0)`)
# (H) and delegated ctors (`: widget(0)`), but no call_expression exists for
# (H) them, so the base ctor of a class only ever constructed through derived
# (H) classes had zero incoming CALLS and reported dead (fmt's buffer.buffer:
# (H) every use goes through container_buffer/counting_buffer member-init).
# (H) Each field_initializer whose head name resolves to a registered class
# (H) emits CALLS from the ctor to that class's ctors; a plain member field
# (H) initializer (`c_(g)`) resolves to no class and emits nothing.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

BASE_INIT_CC = """
struct buffer {
  int g_;
  buffer(int g, int s) : g_(g) {}
};

struct container_buffer : buffer {
  int c_;
  container_buffer(int g) : buffer(g, 0), c_(g) {}
};
"""

DELEGATING_CC = """
struct widget {
  int x_;
  widget(int x) : x_(x) {}
  widget() : widget(0) {}
};
"""

TEMPLATE_BASE_CC = """
template <typename T> struct base {
  base(int g) {}
};

template <typename T> struct child : base<T> {
  child(int g) : base<T>(g) {}
};
"""

QUALIFIED_BASE_CC = """
namespace ns {
struct other {
  other(int v) {}
};
}

struct wrapper : ns::other {
  wrapper() : ns::other(1) {}
};
"""


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
    }


def test_base_class_member_init_emits_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "buf.cc").write_text(BASE_INIT_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".container_buffer.container_buffer")
        and dst.endswith(".buffer.buffer")
        for src, dst in calls
    ), calls


def test_plain_field_init_emits_no_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "buf.cc").write_text(BASE_INIT_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    # (H) `c_(g)` and `g_(g)` are member field inits; nothing named c_/g_ may
    # (H) receive a CALLS edge.
    assert not any(
        dst.rsplit(".", 1)[-1] in ("c_", "g_") for _, dst in calls
    ), calls


def test_delegating_ctor_emits_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "w.cc").write_text(DELEGATING_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".widget.widget") and dst.endswith(".widget.widget")
        for src, dst in calls
    ), calls


def test_template_base_member_init_emits_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "tpl.cc").write_text(TEMPLATE_BASE_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".child.child") and dst.endswith(".base.base")
        for src, dst in calls
    ), calls


def test_qualified_base_member_init_emits_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "q.cc").write_text(QUALIFIED_BASE_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".wrapper.wrapper") and dst.endswith(".other.other")
        for src, dst in calls
    ), calls
