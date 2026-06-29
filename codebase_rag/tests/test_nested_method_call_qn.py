from pathlib import Path

from evals.cgr_graph import _capture


def _make_repo(root: Path) -> None:
    pkg = root / "proj"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(
        "def target():\n    return 1\n\n\n"
        "class C:\n"
        "    def method(self):\n"
        "        def nested():\n"
        "            return target()\n"
        "        return nested()\n",
        encoding="utf-8",
    )


def test_method_nested_function_call_uses_full_caller_qn(tmp_path: Path) -> None:
    # (H) A call inside a function nested in a method must be attributed to that
    # (H) nested function's real node qn (Class.method.nested), not to a
    # (H) method-dropping qn (Class.nested) that matches no node.
    _make_repo(tmp_path)
    ingestor = _capture(tmp_path / "proj", "proj")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    node_qns = {str(uid) for (_label, uid) in ingestor.nodes}

    # (H) the nested function node exists with its full qn
    assert "proj.m.C.method.nested" in node_qns
    # (H) and its outbound call is attributed to that full qn
    assert ("proj.m.C.method.nested", "proj.m.target") in calls
    # (H) never to the malformed method-dropping qn
    assert ("proj.m.C.nested", "proj.m.target") not in calls
