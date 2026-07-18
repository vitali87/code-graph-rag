# (H) `using appender = basic_appender<char>; appender(out)` constructs the
# (H) aliased class, but the alias is no registered node, so the call resolved
# (H) to nothing and the class's ctor kept zero incoming edges (fmt's
# (H) basic_appender.basic_appender dead-list residual from PR #791). A bare
# (H) unresolved C++ call name that the collected typedef/using alias map
# (H) resolves to a registered class is a construction: INSTANTIATES + ctor
# (H) CALLS, exactly like a direct class-name construction.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

USING_ALIAS_CC = """
struct basic_appender {
  basic_appender(int b) {}
};
using appender = basic_appender;

void use_it() {
  auto a = appender(1);
}
"""

TEMPLATE_ALIAS_CC = """
template <typename T> struct basic_view {
  basic_view(T v) {}
};
using view = basic_view<int>;

void use_view() {
  auto v = view(2);
}
"""

TYPEDEF_ALIAS_CC = """
struct mutex_impl {
  mutex_impl(int m) {}
};
typedef mutex_impl mutex_t;

void lock_it() {
  auto m = mutex_t(3);
}
"""

UNRELATED_ALIAS_CC = """
using count_t = int;

void tally() {
  auto c = count_t(4);
}
"""


def _edges(mock_ingestor: MagicMock, rel: cs.RelationshipType) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel.value
    }


def test_using_alias_construction_emits_instantiates_and_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "a.cc").write_text(USING_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    inst = _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES)
    assert any(
        src.endswith(".use_it") and dst.endswith(".basic_appender.basic_appender")
        for src, dst in calls
    ), calls
    assert any(
        src.endswith(".use_it") and dst.endswith(".basic_appender")
        for src, dst in inst
    ), inst


def test_template_alias_construction_emits_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "v.cc").write_text(TEMPLATE_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert any(
        src.endswith(".use_view") and dst.endswith(".basic_view.basic_view")
        for src, dst in calls
    ), calls


def test_typedef_alias_construction_emits_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.cc").write_text(TYPEDEF_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert any(
        src.endswith(".lock_it") and dst.endswith(".mutex_impl.mutex_impl")
        for src, dst in calls
    ), calls


def test_alias_to_non_class_emits_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "c.cc").write_text(UNRELATED_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    inst = _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES)
    # (H) `count_t(4)` is a primitive functional cast; the alias resolves to no
    # (H) registered class and must stay edge-free.
    assert not any("count_t" in dst or "int" in dst for _, dst in calls | inst), (
        calls | inst
    )
