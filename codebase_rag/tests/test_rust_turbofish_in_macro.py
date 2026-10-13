"""A Rust turbofish call inside a macro gets the edge it gets outside one.

A macro body is a flat token stream, so `make::<u64>()` reads
`make :: < u64 > ( )`, and the call query only captured an identifier
immediately followed by its argument group. Every turbofish call inside
`assert!` / `assert_eq!` / `println!` lost its edge, free function and
method alike (issue #3212; serde_json: 29 of 85 `from_str` sites, all tests).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_LIB = """\
pub struct Ext { pub m: u64 }
impl Ext {
    pub fn into_float<T: From<u32>>(&self) -> T { T::from(1) }
    pub fn assoc() -> u64 { 0 }
}
pub struct Holder<T> { pub v: T }
impl<T: Default> Holder<T> {
    pub fn build() -> u8 { 1 }
}
pub fn make<T: Default>() -> T { T::default() }
pub fn parse<T: Default>(_s: &str) -> T { T::default() }
pub fn plain_fn() -> u64 { 0 }
pub fn in_macro() {
    assert!(make::<u64>() == 0);
    assert_eq!(make::<u32>(), 0);
    println!("{}", make::<u8>());
}
pub fn nested_generics() { assert!(parse::<Vec<Vec<u8>>>("x").is_empty()); }
pub fn method_turbo_in_macro() { let x = Ext { m: 0 }; assert!(x.into_float::<f64>() > 0.0); }
pub fn in_macro_plain() { assert!(plain_fn() == 0); }
pub fn assoc_in_macro() { assert_eq!(Ext::assoc(), 0); }
pub fn path_turbofish() { assert_eq!(Holder::<u8>::build(), 1); }
pub fn outside() -> u64 { make::<u64>() }
"""

_Sites = list[tuple[str, str, int]]


@pytest.fixture(scope="module")
def sites(tmp_path_factory: pytest.TempPathFactory) -> _Sites:
    root = tmp_path_factory.mktemp("rs3212") / "tb"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "tb"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(_LIB, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="rust")
    out: _Sites = []
    for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.INSTANTIATES):
        for c in get_relationships(mock, rel):
            props = c.kwargs.get("properties") or {}
            out.append(
                (
                    str(c.args[0][2]).rsplit(".", 1)[1],
                    str(c.args[2][2]).split(".src.lib.", 1)[1],
                    int(props[cs.KEY_LINE]),
                )
            )
    return sorted(out)


def test_turbofish_calls_inside_macros_get_their_edges(sites: _Sites) -> None:
    assert [s for s in sites if s[0] == "in_macro"] == [
        ("in_macro", "make", 14),
        ("in_macro", "make", 15),
        ("in_macro", "make", 16),
    ], sites


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("method_turbo_in_macro", "Ext.into_float"),
        ("nested_generics", "parse"),
    ],
    ids=["method", "nested-generics-closing-with-shift-token"],
)
def test_a_turbofish_method_or_nested_generic_call_resolves(
    sites: _Sites, caller: str, callee: str
) -> None:
    assert [to for src, to, _line in sites if src == caller] == [callee], sites


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("in_macro_plain", "plain_fn"),
        ("assoc_in_macro", "Ext.assoc"),
        ("outside", "make"),
    ],
    ids=["plain-call-in-macro", "assoc-in-macro", "turbofish-outside-a-macro"],
)
def test_calls_that_already_resolved_still_do(
    sites: _Sites, caller: str, callee: str
) -> None:
    # Negatives: a plain call in a macro, #2698's `Type::assoc()`, and a
    # turbofish call outside any macro are unchanged.
    assert [to for src, to, _line in sites if src == caller] == [callee], sites


def test_a_turbofish_path_segment_is_not_a_call(sites: _Sites) -> None:
    # Negative: in `Holder::<u8>::build()` the turbofish continues the path,
    # so the struct `Holder` is neither called nor constructed there.
    assert ("path_turbofish", "Holder") not in {(s, t) for s, t, _ in sites}, sites
