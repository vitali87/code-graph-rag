# (H) `View.prototype.lookup = function lookup(...) {...}` registers TWO nodes for
# (H) one method (the prototype path's `View.lookup` and the fn-expr's own-name
# (H) module-flat `view.lookup`). A call binds one twin and the other reports dead.
# (H) Per the duplicate-QN design (keep both nodes, CALLS-to-both), a dotted call
# (H) that binds either twin must also edge the same-module same-name member twin.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_member_call_edges_both_prototype_twins(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "exproto"
    root.mkdir(parents=True)
    (root / "view.js").write_text(
        "function View(name) {\n"
        "  this.name = name\n"
        "  this.lookup(name)\n"
        "}\n"
        "View.prototype.lookup = function lookup(name) {\n"
        "  return name\n"
        "}\n"
        "module.exports = View\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(f.endswith(".View") and t.endswith(".View.lookup") for f, t in calls), (
        sorted(t for f, t in calls if "lookup" in t)
    )
