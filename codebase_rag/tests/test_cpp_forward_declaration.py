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
