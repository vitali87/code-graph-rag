from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    get_nodes,
    get_qualified_names,
    run_updater,
)

# (H) A C++ forward declaration (`class Widget;`) is a bodyless class_specifier. The
# (H) definition pass registered it as its own Class node (zero methods), so the real
# (H) definition that followed collided on the qn and was suffixed (`Widget@<line>`),
# (H) fragmenting one class into several same-named nodes across files. That made
# (H) member-call resolution pick among duplicate candidates (a correctness bug) and,
# (H) via hash-ordered candidate selection, produced non-reproducible graphs. A
# (H) forward declaration must NOT create a Class node; only the real definition does.
CPP_SOURCE = """
namespace ns {

class Widget;

class Widget {
 public:
  int run() { return 1; }
};

int use(Widget* w) { return w->run(); }

}  // namespace ns
"""


def test_forward_declaration_does_not_create_phantom_class(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_fwd"
    project.mkdir()
    (project / "w.cpp").write_text(CPP_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    class_qns = get_qualified_names(get_nodes(mock_ingestor, "Class"))
    widgets = [q for q in class_qns if q.rsplit(".", 1)[-1].startswith("Widget")]
    assert len(widgets) == 1, f"expected exactly one Widget Class node, got {widgets}"

    # (H) The single surviving node is the real definition, so its qn carries no
    # (H) collision suffix and its method registers cleanly under it.
    method_qns = get_qualified_names(get_nodes(mock_ingestor, "Method"))
    assert any(q.endswith(".ns.Widget.run") for q in method_qns), (
        f"expected ns.Widget.run method node, got {sorted(method_qns)}"
    )


# (H) A template class forward declaration (`template <T> class Box;`) is a
# (H) template_declaration wrapping a bodyless class_specifier, so the plain guard on
# (H) class_specifier node type missed it and it still fragmented the class. It must
# (H) be dropped the same way -- BUT only when a real definition exists, because a
# (H) primary template forward-declared and defined solely via specializations is the
# (H) canonical node and must be kept. The invariant: a template forward declaration
# (H) that is followed by a real definition adds no Box node beyond the definition's.
_TEMPLATE_DEF_ONLY = """
namespace ns {
template <typename T>
class Box {
 public:
  T get() { return value_; }
  T value_;
};
}  // namespace ns
"""

_TEMPLATE_FORWARD_PLUS_DEF = """
namespace ns {
template <typename T>
class Box;
template <typename T>
class Box {
 public:
  T get() { return value_; }
  T value_;
};
}  // namespace ns
"""


def _box_class_count(mock_ingestor: MagicMock) -> int:
    return len(
        [
            q
            for q in get_qualified_names(get_nodes(mock_ingestor, "Class"))
            if q.rsplit(".", 1)[-1].startswith("Box")
        ]
    )


def test_template_forward_declaration_adds_no_node(temp_repo: Path) -> None:
    baseline_repo = temp_repo / "def_only"
    baseline_repo.mkdir()
    (baseline_repo / "d.cpp").write_text(_TEMPLATE_DEF_ONLY, encoding="utf-8")
    baseline_ingestor = MagicMock()
    run_updater(baseline_repo, baseline_ingestor)
    baseline = _box_class_count(baseline_ingestor)
    assert baseline >= 1, "definition-only template produced no Box node"

    with_forward_repo = temp_repo / "fwd_and_def"
    with_forward_repo.mkdir()
    (with_forward_repo / "f.cpp").write_text(
        _TEMPLATE_FORWARD_PLUS_DEF, encoding="utf-8"
    )
    with_forward_ingestor = MagicMock()
    run_updater(with_forward_repo, with_forward_ingestor)
    with_forward = _box_class_count(with_forward_ingestor)

    assert with_forward == baseline, (
        f"template forward declaration added {with_forward - baseline} phantom "
        f"Box node(s) (baseline {baseline}, with forward {with_forward})"
    )
