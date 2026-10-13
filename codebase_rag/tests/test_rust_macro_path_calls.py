"""A Rust `Type::assoc()` call inside a macro gets the edge it gets outside one.

tree-sitter-rust gives a macro body as a flat token_tree, so `Config::load()`
inside `vec![...]` is the tokens `Config`, `::`, `load`, `( )`. The call name
was rebuilt only across `.`, so it stayed the bare `load`, which never binds
to a method; anything chained on it (`Circle::new(2.0).area()`) was lost too.
Associated functions called only from `vec!`, `assert_eq!`, `println!` or
`format!` were reported dead and no test reached them (issue #2698).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_MAIN = """\
mod util;

use std::collections::HashMap;

pub struct Config {
    pub name: String,
}

impl Config {
    fn load() -> Config {
        Config { name: Config::default_name() }
    }
    fn default_name() -> String {
        "app".to_string()
    }
}

pub struct Circle {
    pub r: f64,
}

impl Circle {
    pub fn new(r: f64) -> Circle {
        Circle { r }
    }
    pub fn unit() -> Self {
        Self::new(1.0)
    }
    pub fn pair() -> Vec<Circle> {
        vec![Self::new(1.0), Self::new(2.0)]
    }
    pub fn scaled(&self, k: f64) -> Circle {
        Circle { r: self.r * k }
    }
    pub fn area(&self) -> f64 {
        3.14 * self.r * self.r
    }
    pub fn from(s: &str) -> Circle {
        Circle { r: s.len() as f64 }
    }
}

pub enum Shape {
    Round(f64),
}

fn describe(c: &Circle) -> String {
    format!("{}", c.r)
}

fn inner() -> u32 {
    1
}

fn in_vec() -> usize {
    vec![Config::load()].len()
}

fn chained_in_println() {
    println!("{}", Circle::unit().scaled(2.0).area());
}

fn ctor_then_method() {
    println!("{}", Circle::new(2.0).area());
}

fn ctor_then_field() {
    println!("{}", Circle::new(1.0).r);
}

fn in_assert() {
    assert!(Circle::unit().area() > 0.0);
}

fn in_format() -> String {
    format!("{:.1}", Circle::unit().area())
}

fn two_in_vec() -> usize {
    vec![Circle::unit(), Circle::new(3.0)].len()
}

fn nested_in_bare_call() {
    println!("{}", describe(&Circle::unit()));
}

fn module_paths() {
    println!("{} {}", crate::util::helper(), util::helper());
}

fn outside_a_macro() -> f64 {
    Circle::new(2.0).area()
}

fn bare_method_name() {
    println!("{}", area());
}

fn variant_pattern(s: Shape) -> bool {
    matches!(s, Shape::Round(_))
}

fn library_types() {
    println!("{:?} {}", HashMap::<u8, u8>::new(), String::from("x"));
}

fn main() {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loads_default_name() {
        assert_eq!(Config::load().name, "app");
    }
}
"""
_UTIL = """\
pub fn helper() -> u32 {
    super::inner()
}
"""

_Calls = dict[str, dict[str, str]]


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    root = tmp_path_factory.mktemp("rs2698") / "rsmacro"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsmacro"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / "src" / "main.rs").write_text(_MAIN, encoding="utf-8")
    (root / "src" / "util.rs").write_text(_UTIL, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="rust")
    out: _Calls = {}
    for c in get_relationships(mock, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or {}
        caller = str(c.args[0][2]).split(".src.", 1)[-1]
        callee = str(c.args[2][2]).split(".src.", 1)[-1]
        out.setdefault(caller, {})[callee] = str(props.get(cs.KEY_RESOLUTION))
    return out


@pytest.mark.parametrize(
    ("caller", "callees"),
    [
        ("main.in_vec", {"main.Config.load"}),
        ("main.tests.loads_default_name", {"main.Config.load"}),
        (
            "main.chained_in_println",
            {"main.Circle.unit", "main.Circle.scaled", "main.Circle.area"},
        ),
        ("main.ctor_then_method", {"main.Circle.new", "main.Circle.area"}),
        ("main.ctor_then_field", {"main.Circle.new"}),
        ("main.in_assert", {"main.Circle.unit", "main.Circle.area"}),
        ("main.in_format", {"main.Circle.unit", "main.Circle.area"}),
        ("main.two_in_vec", {"main.Circle.unit", "main.Circle.new"}),
        ("main.nested_in_bare_call", {"main.describe", "main.Circle.unit"}),
        ("main.Circle.pair", {"main.Circle.new"}),
        ("main.module_paths", {"util.helper"}),
    ],
)
def test_a_path_call_in_a_macro_binds_exact(
    calls: _Calls, caller: str, callees: set[str]
) -> None:
    assert calls.get(caller) == dict.fromkeys(callees, "exact"), calls


def test_the_same_chain_outside_a_macro_binds_exact(calls: _Calls) -> None:
    # The `.` inside `new(2.0)` split the chain mid-argument, so no hop held
    # both parens and `area` fell to a `heuristic` match by name.
    assert calls.get("main.outside_a_macro") == {
        "main.Circle.new": "exact",
        "main.Circle.area": "exact",
    }, calls


def test_the_repro_leaves_nothing_dead(calls: _Calls) -> None:
    called = {callee for targets in calls.values() for callee in targets}
    assert {"main.Config.load", "main.Config.default_name"} <= called, calls
    assert "main.Circle.from" not in called, calls


def test_names_a_macro_does_not_call_stay_unbound(calls: _Calls) -> None:
    # Negatives: a bare method name binds to no method (issue #1011), an enum
    # variant pattern is not a function, and a library type's `new` / `from`
    # is not the project's method of that name (`Circle::from` exists).
    for caller in (
        "main.bare_method_name",
        "main.variant_pattern",
        "main.library_types",
    ):
        assert caller not in calls, calls
