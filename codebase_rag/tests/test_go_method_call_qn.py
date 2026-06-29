from pathlib import Path

import pytest

from evals.cgr_graph import _capture
from evals.oracles import go_available

needs_go = pytest.mark.skipif(not go_available(), reason="go toolchain not installed")


def _make_repo(root: Path) -> None:
    pkg = root / "p"
    pkg.mkdir(parents=True)
    (pkg / "m.go").write_text(
        "package p\n\n"
        "type T struct{}\n\n"
        "func free() int { return 1 }\n\n"
        "func (t T) callsFree() int { return free() }\n",
        encoding="utf-8",
    )


@needs_go
def test_go_method_call_caller_qn_includes_receiver(tmp_path: Path) -> None:
    # (H) A call inside a Go receiver method must be attributed to the method's
    # (H) real node qn (p.m.T.callsFree), which binds to the receiver type, not a
    # (H) receiver-dropping qn (p.m.callsFree) that matches no node.
    _make_repo(tmp_path)
    ingestor = _capture(tmp_path / "p", "p")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    node_qns = {str(uid) for (_label, uid) in ingestor.nodes}

    assert "p.m.T.callsFree" in node_qns
    assert ("p.m.T.callsFree", "p.m.free") in calls
    assert ("p.m.callsFree", "p.m.free") not in calls
