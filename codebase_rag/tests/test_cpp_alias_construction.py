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
        src.endswith(".use_it") and dst.endswith(".basic_appender") for src, dst in inst
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


LOCAL_ALIAS_CC = """
struct basic_writer {
  basic_writer(int b) {}
};

void write_it() {
  using writer = basic_writer;
  auto w = writer(1);
}

void type_it() {
  typedef basic_writer writer_t;
  auto w = writer_t(2);
}
"""


def test_function_local_alias_construction_emits_ctor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "l.cc").write_text(LOCAL_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    # (H) The cross-file alias collector deliberately skips function bodies, so
    # (H) a body-local `using writer = basic_writer;` needs the caller-scoped
    # (H) map (PR #797 review).
    assert any(
        src.endswith(".write_it") and dst.endswith(".basic_writer.basic_writer")
        for src, dst in calls
    ), calls
    assert any(
        src.endswith(".type_it") and dst.endswith(".basic_writer.basic_writer")
        for src, dst in calls
    ), calls


LATE_ALIAS_CC = """
struct real_thing {
  real_thing(int x) {}
};

void confused() {
  later_alias(1);
  using later_alias = real_thing;
}
"""


def test_alias_declared_after_call_site_does_not_bind(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "late.cc").write_text(LATE_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    # (H) C++ name lookup is declaration-ordered: `later_alias(1)` precedes the
    # (H) using-declaration and can never mean real_thing (PR #797 review).
    assert not any(
        src.endswith(".confused") and "real_thing" in dst for src, dst in calls
    ), calls


SCOPED_ALIAS_CC = """
struct real_widget {
  real_widget(int x) {}
};

void blocked() {
  {
    using widget_t = real_widget;
  }
  widget_t(1);
}

void lambda_leak() {
  auto fn = [] {
    using lam_t = real_widget;
  };
  lam_t(2);
}

void inside() {
  {
    using in_t = real_widget;
    auto w = in_t(3);
  }
}
"""


def test_alias_out_of_lexical_scope_does_not_bind(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "scoped.cc").write_text(SCOPED_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    # (H) A block/lambda-local alias dies at its closing brace: a later call in
    # (H) the enclosing scope can never mean it (PR #797 review round 4).
    assert not any(
        src.endswith(".blocked") and "real_widget" in dst for src, dst in calls
    ), calls
    assert not any(
        src.endswith(".lambda_leak") and "real_widget" in dst for src, dst in calls
    ), calls
    # (H) inside its own block the alias still binds
    assert any(
        src.endswith(".inside") and dst.endswith(".real_widget.real_widget")
        for src, dst in calls
    ), calls


DISJOINT_ALIAS_CC = """
struct real_one {
  real_one(int x) {}
};
struct real_two {
  real_two(int x) {}
};

void two_blocks() {
  {
    using block_t = real_one;
    auto a = block_t(1);
  }
  {
    using block_t = real_two;
    auto b = block_t(2);
  }
}

void shadowed() {
  using sh_t = real_one;
  {
    using sh_t = real_two;
    auto s = sh_t(3);
  }
  auto o = sh_t(4);
}
"""


def test_same_name_aliases_in_disjoint_scopes_both_bind(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "disjoint.cc").write_text(DISJOINT_ALIAS_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _edges(mock_ingestor, cs.RelationshipType.CALLS)
    # (H) disjoint blocks each own their alias: neither is a conflict of the
    # (H) other, and each call binds to its own block's target (PR #797 review
    # (H) round 5).
    assert any(
        src.endswith(".two_blocks") and dst.endswith(".real_one.real_one")
        for src, dst in calls
    ), calls
    assert any(
        src.endswith(".two_blocks") and dst.endswith(".real_two.real_two")
        for src, dst in calls
    ), calls
    # (H) shadowing: the innermost alias wins inside its block, the outer one
    # (H) resumes after the block closes
    assert any(
        src.endswith(".shadowed") and dst.endswith(".real_two.real_two")
        for src, dst in calls
    ), calls
    assert any(
        src.endswith(".shadowed") and dst.endswith(".real_one.real_one")
        for src, dst in calls
    ), calls
