# A Rust closure is a value constructed where it is written (a `.map(|x| ..)`
# arg, a spawn body, a `let` binding). For dead-code reachability (which walks
# CALLS/REFERENCES, not DEFINES), the closure must carry an incoming REFERENCES
# edge from its enclosing function -- else every closure is an orphan and reports
# as dead. This mirrors the inline-callback REFERENCES edge JS/TS already emit.
from pathlib import Path

from evals.cgr_graph import _capture


def _refs(ingestor) -> set[tuple[str, str]]:
    return {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "REFERENCES"
    }


def _closure_nodes(ingestor) -> set[str]:
    return {str(uid) for _label, uid in ingestor.nodes if "anonymous_" in str(uid)}


def _make_crate(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lib.rs").write_text(body, encoding="utf-8")


def test_closure_arg_referenced_by_enclosing_fn(tmp_path: Path) -> None:
    # `xs.iter().map(|s| s.len())` inside a free fn: the closure must be
    # referenced by the enclosing function so it is reachable, not orphaned.
    _make_crate(
        tmp_path,
        "pub fn run(xs: Vec<String>) -> usize {\n"
        "    xs.iter().map(|s| s.len()).sum()\n"
        "}\n",
    )
    ingestor = _capture(tmp_path, "crate")
    refs = _refs(ingestor)
    closures = _closure_nodes(ingestor)
    assert closures, "expected a closure node to be registered"
    for closure in closures:
        assert any(to == closure and frm == "crate.lib.run" for frm, to in refs), (
            f"closure {closure} has no REFERENCES edge from its enclosing fn"
        )


def test_closure_in_method_referenced(tmp_path: Path) -> None:
    # A closure in an impl method body must be referenced by that method.
    _make_crate(
        tmp_path,
        "pub struct Db {}\n"
        "impl Db {\n"
        "    pub fn purge(&self, xs: Vec<i32>) -> i32 {\n"
        "        xs.iter().map(|x| x + 1).sum()\n"
        "    }\n"
        "}\n",
    )
    ingestor = _capture(tmp_path, "crate")
    refs = _refs(ingestor)
    closures = _closure_nodes(ingestor)
    assert closures
    for closure in closures:
        assert any(to == closure and frm == "crate.lib.Db.purge" for frm, to in refs), (
            f"closure {closure} has no REFERENCES edge from Db.purge"
        )
