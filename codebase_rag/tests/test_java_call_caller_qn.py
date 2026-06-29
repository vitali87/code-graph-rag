from pathlib import Path

from evals.cgr_graph import _capture


def _make_file(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "T.java").write_text(
        "class T {\n"
        "    int helper() { return 1; }\n"
        "    int caller() { return this.helper(); }\n"
        "}\n",
        encoding="utf-8",
    )


def test_java_method_caller_qn_carries_signature(tmp_path: Path) -> None:
    # (H) The definition pass registers a Java method node with its parameter
    # (H) signature (demo.T.T.caller()), but the call pass built the caller qn
    # (H) without it (demo.T.T.caller) -> the CALLS from-endpoint matched no node
    # (H) and the edge would not attach in Memgraph. The caller qn must carry the
    # (H) same signature as the registered Method node.
    _make_file(tmp_path)
    ingestor = _capture(tmp_path, "demo")
    node_qns = {str(uid) for (_label, uid) in ingestor.nodes}
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }

    assert "demo.T.T.caller()" in node_qns
    assert ("demo.T.T.caller()", "demo.T.T.helper()") in calls
    assert ("demo.T.T.caller", "demo.T.T.helper()") not in calls
