from pathlib import Path

from evals.cgr_graph import _capture


def _make_crate(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lib.rs").write_text(
        "pub struct Chars<'a> { s: &'a str }\n\n"
        "impl<'a> Chars<'a> {\n"
        "    pub fn as_str(&self) -> &'a str { self.s }\n"
        "}\n\n"
        "pub trait Thing { fn go(&self) -> usize; }\n\n"
        "impl<'a> Thing for Chars<'a> {\n"
        "    fn go(&self) -> usize { self.as_str().len() }\n"
        "}\n",
        encoding="utf-8",
    )


def test_rust_generic_impl_method_caller_qn_strips_generics(tmp_path: Path) -> None:
    # A method in a generic impl block (`impl<'a> Thing for Chars<'a>`) registers
    # on the bare type node (crate.lib.Chars.go). The call inside must attribute
    # to that bare-type caller qn, not a generic-bearing crate.lib.Chars<'a>.go
    # that matches no node and drops the CALLS edge.
    _make_crate(tmp_path)
    ingestor = _capture(tmp_path, "crate")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    node_qns = {str(uid) for (_label, uid) in ingestor.nodes}

    assert "crate.lib.Chars.go" in node_qns
    assert ("crate.lib.Chars.go", "crate.lib.Chars.as_str") in calls
    assert ("crate.lib.Chars<'a>.go", "crate.lib.Chars.as_str") not in calls


def _make_super_import_crate(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lib.rs").write_text("pub mod a;\npub mod b;\n", encoding="utf-8")
    (root / "b.rs").write_text("pub fn helper() -> i32 { 1 }\n", encoding="utf-8")
    (root / "a.rs").write_text(
        "use super::b::helper;\n\npub fn run() -> i32 { helper() }\n",
        encoding="utf-8",
    )


def test_rust_super_imported_free_fn_call_resolves(tmp_path: Path) -> None:
    # A free function imported by a relative path (`use super::b::helper`) and
    # called bare must resolve to the sibling-module node (crate.b.helper). Its
    # raw import target `super::b::helper` is `::`-separated and not project-prefixed,
    # so the external-import guard must not suppress the trie fallback.
    _make_super_import_crate(tmp_path)
    ingestor = _capture(tmp_path, "crate")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }

    assert ("crate.a.run", "crate.b.helper") in calls
