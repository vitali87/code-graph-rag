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


def _make_cross_package(root: Path) -> None:
    (root / "pkga").mkdir(parents=True, exist_ok=True)
    (root / "pkgb").mkdir(parents=True, exist_ok=True)
    (root / "pkgb" / "T.java").write_text(
        "package pkgb;\npublic class T {\n    public static int make() { return 1; }\n}\n",
        encoding="utf-8",
    )
    # (H) Use references bare `T.make()` with NO import; in Java this only compiles
    # (H) for a same-package or imported T, never a class in another package.
    (root / "pkga" / "Use.java").write_text(
        "package pkga;\nclass Use {\n    int run() { return T.make(); }\n}\n",
        encoding="utf-8",
    )


def test_java_unimported_cross_package_static_call_does_not_resolve(
    tmp_path: Path,
) -> None:
    # (H) A bare class-name receiver with no import must not resolve to a class in
    # (H) a different package (directory): the same-package fallback is exhausted,
    # (H) so leave the receiver unlinked rather than emit a wrong cross-package edge.
    _make_cross_package(tmp_path)
    ingestor = _capture(tmp_path, "demo")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    assert not any(to_val.endswith("T.make()") for _f, to_val in calls)
