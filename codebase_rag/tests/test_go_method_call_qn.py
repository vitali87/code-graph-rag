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
    # A call inside a Go receiver method must be attributed to the method's
    # real node qn (p.m.T.callsFree), which binds to the receiver type, not a
    # receiver-dropping qn (p.m.callsFree) that matches no node.
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


def _make_dispatch_repo(root: Path) -> None:
    pkg = root / "p"
    pkg.mkdir(parents=True)
    (pkg / "m.go").write_text(
        "package p\n\n"
        "type T struct{}\n\n"
        "func (t T) helper() int { return 1 }\n\n"
        "func (t T) caller() int { return t.helper() }\n\n"
        "func use(v T) int { return v.helper() }\n\n"
        "func make_local() int {\n"
        "\tx := T{}\n"
        "\treturn x.helper()\n"
        "}\n",
        encoding="utf-8",
    )


@needs_go
def test_go_receiver_method_dispatch_resolves(tmp_path: Path) -> None:
    # A method call on a Go receiver (`t.helper()`), a typed parameter
    # (`v.helper()`), and a composite-literal local (`x := T{}; x.helper()`)
    # must each resolve to the method node `p.m.T.helper` via local-variable
    # type inference, not be dropped for lack of a typed receiver.
    _make_dispatch_repo(tmp_path)
    ingestor = _capture(tmp_path / "p", "p")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }

    assert ("p.m.T.caller", "p.m.T.helper") in calls
    assert ("p.m.use", "p.m.T.helper") in calls
    assert ("p.m.make_local", "p.m.T.helper") in calls
