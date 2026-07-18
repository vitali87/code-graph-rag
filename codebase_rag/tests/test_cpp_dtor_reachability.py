# (H) Constructing a C++ object guarantees its destructor runs at end of
# (H) lifetime, but no call node ever names `~X`, so a dtor whose class is
# (H) constructed everywhere still reported dead (fmt's args node.~node).
# (H) Every construction site that redirects CALLS to the class's ctors now
# (H) redirects to its destructor too: call-expression constructions,
# (H) member-initializer base/delegated ctor runs, and braced returns.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

CONSTRUCTED_DTOR_CC = """
struct widget {
  int x_;
  widget(int x) : x_(x) {}
  ~widget() {}
};

void use_widget() {
  auto w = widget(1);
}
"""

MEMBER_INIT_DTOR_CC = """
struct buffer {
  buffer(int g) {}
  ~buffer() {}
};

struct container_buffer : buffer {
  container_buffer(int g) : buffer(g) {}
};
"""

UNCONSTRUCTED_DTOR_CC = """
struct orphaned {
  orphaned(int x) {}
  ~orphaned() {}
};

void unrelated() {}
"""

BRACED_RETURN_DTOR_CC = """
struct error_box {
  error_box(int code) {}
  ~error_box() {}
};

error_box make_error(int code) {
  return {code};
}
"""


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
    }


def test_construction_call_reaches_dtor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "w.cc").write_text(CONSTRUCTED_DTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".use_widget") and dst.endswith(".widget.~widget")
        for src, dst in calls
    ), calls


def test_member_init_base_reaches_base_dtor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "b.cc").write_text(MEMBER_INIT_DTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    # (H) A derived object's destruction runs the base dtor; the derived ctor
    # (H) is the construction site the graph can see.
    assert any(
        src.endswith(".container_buffer.container_buffer")
        and dst.endswith(".buffer.~buffer")
        for src, dst in calls
    ), calls


def test_unconstructed_class_dtor_gets_no_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "o.cc").write_text(UNCONSTRUCTED_DTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert not any("~orphaned" in dst for _, dst in calls), calls


def test_braced_return_reaches_dtor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "e.cc").write_text(BRACED_RETURN_DTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".make_error") and dst.endswith(".error_box.~error_box")
        for src, dst in calls
    ), calls
