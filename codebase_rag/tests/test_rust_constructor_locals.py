"""A Rust local built by any constructor, or typed `Self`, types its calls.

Only a struct literal (`Point { x: 1 }`) and an associated call
(`Type::new()`) typed a `let`. A unit or tuple struct, an enum variant,
any of them behind `&`, `Self { .. }` and a `Self`-typed parameter or
annotation left the receiver untyped, so `p.render()` bound a same-named
method of another type (heuristic) or nothing at all (issue #3168).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_LIB = """\
pub mod shapes;
pub mod rival;
pub mod paths;
pub struct Widget;
pub struct Pair(pub u32, pub u32);
pub struct Point { pub x: u32 }
pub enum Shape { Circle(u32), Unit, Rect { w: u32 } }
pub struct Other;
// Shares its name with the `Shape::Circle` variant.
pub struct Circle;
impl Circle { pub fn render(&self) -> u32 { 7 } }
impl Other { pub fn render(&self) -> u32 { 9 } }
impl Widget { pub fn render(&self) -> u32 { 1 } }
impl Pair { pub fn render(&self) -> u32 { self.0 } }
impl Shape {
    pub fn render(&self) -> u32 { 2 }
    pub fn self_variant() -> u32 { let s = Self::Circle(1); s.render() }
}
impl Point {
    pub fn render(&self) -> u32 { self.x }
    pub fn new() -> Self { let p = Self { x: 0 }; p.render(); p }
    pub fn copy_of(&self, other: &Self) -> u32 { other.render() }
    pub fn annotated(&self) -> u32 { let s: Self = Point { x: 2 }; s.render() }
    pub fn make() -> Point { Point { x: 5 } }
    pub fn self_assoc() -> u32 { let p = Self::make(); p.render() }
}
pub fn brace() -> u32 { let p = Point { x: 1 }; p.render() }
pub fn unit() -> u32 { let w = Widget; w.render() }
pub fn tuple() -> u32 { let p = Pair(1, 2); p.render() }
pub fn variant() -> u32 { let s = Shape::Circle(1); s.render() }
pub fn unit_variant() -> u32 { let s = Shape::Unit; s.render() }
pub fn struct_variant() -> u32 { let s = Shape::Rect { w: 1 }; s.render() }
pub fn by_ref() -> u32 { let p = &Point { x: 1 }; p.render() }
pub fn by_mut_ref() -> u32 { let p = &mut Pair(1, 2); p.render() }
pub fn assoc() -> u32 { let p = Point::make(); p.render() }
pub fn module_path() -> u32 { let c = shapes::Disk { r: 1 }; c.area() }
pub struct Product;
impl Product { pub fn render(&self) -> u32 { 3 } }
pub struct Factory;
impl Factory {
    #[allow(non_snake_case)]
    pub fn Build() -> Product { Product }
    pub fn render(&self) -> u32 { 4 }
    pub fn self_pascal_fn() -> u32 { let p = Self::Build(); p.render() }
}
pub struct S { pub x: u32 }
impl S { pub fn render(&self) -> u32 { 10 } }
#[allow(non_camel_case_types)]
pub struct lower { pub x: u32 }
impl lower { pub fn render(&self) -> u32 { 11 } }
pub struct Wrapper<T> { pub v: T }
impl<T> Wrapper<T> {
    pub fn render(&self) -> u32 { 5 }
    #[allow(non_snake_case)]
    pub fn Build() -> Product { Product }
}
pub enum Tree<T> { Leaf(T), Empty }
impl<T> Tree<T> { pub fn render(&self) -> u32 { 6 } }
// Shares its name with the `Tree::Leaf` variant.
pub struct Leaf;
impl Leaf { pub fn render(&self) -> u32 { 8 } }
pub fn pascal_fn() -> u32 { let p = Factory::Build(); p.render() }
pub fn turbofish_pascal_fn() -> u32 { let p = Wrapper::<u8>::Build(); p.render() }
pub fn short_name() -> u32 { let s = S { x: 1 }; s.render() }
pub fn lowercase_name() -> u32 { let l = lower { x: 1 }; l.render() }
pub fn turbofish_literal() -> u32 { let w = Wrapper::<u8> { v: 1 }; w.render() }
pub fn turbofish_variant() -> u32 { let t = Tree::<u8>::Leaf(1); t.render() }
#[allow(clippy::upper_case_acronyms)]
pub struct ID(pub u32);
impl ID { pub fn render(&self) -> u32 { self.0 } }
pub enum Value { I64(i64), Nil }
impl Value { pub fn render(&self) -> u32 { 12 } }
// Shares its name with the `Value::I64` variant.
pub struct I64;
impl I64 { pub fn render(&self) -> u32 { 13 } }
#[allow(non_snake_case)]
pub fn MAKE() -> Product { Product }
pub fn all_caps_tuple() -> u32 { let i = ID(1); i.render() }
pub fn all_caps_variant() -> u32 { let v = Value::I64(1); v.render() }
pub fn all_caps_fn() -> u32 { let p = MAKE(); p.render() }
"""
_SHAPES = """\
pub struct Disk { pub r: u32 }
impl Disk { pub fn area(&self) -> u32 { self.r } }
"""
_RIVAL = """\
pub struct Disk;
impl Disk { pub fn area(&self) -> u32 { 0 } }
"""
# The impl names `shapes::Disk` by path, while the bare `Disk` in this file
# is the imported `rival::Disk`.
_PATHS = """\
use crate::rival::Disk;
impl crate::shapes::Disk {
    pub fn literal() -> u32 { let d = Self { r: 1 }; d.area() }
    pub fn param(&self, other: &Self) -> u32 { other.area() }
    pub fn annotated() -> u32 { let d: Self = crate::shapes::Disk { r: 2 }; d.area() }
}
pub fn rival() -> u32 { let d = Disk; d.area() }
"""

_Calls = dict[tuple[str, str], str]


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    root = tmp_path_factory.mktemp("rs3168") / "rsctor"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsctor"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(_LIB, encoding="utf-8")
    (root / "src" / "shapes.rs").write_text(_SHAPES, encoding="utf-8")
    (root / "src" / "rival.rs").write_text(_RIVAL, encoding="utf-8")
    (root / "src" / "paths.rs").write_text(_PATHS, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="rust")
    return {
        (
            str(c.args[0][2]).split(".src.", 1)[1],
            str(c.args[2][2]).split(".src.", 1)[1],
        ): str((c.kwargs.get("properties") or {}).get(cs.KEY_RESOLUTION))
        for c in get_relationships(mock, cs.RelationshipType.CALLS)
    }


def _callees(calls: _Calls, caller: str) -> dict[str, str]:
    return {to: res for (src, to), res in calls.items() if src == caller}


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("lib.unit", "lib.Widget.render"),
        ("lib.tuple", "lib.Pair.render"),
        ("lib.variant", "lib.Shape.render"),
        ("lib.unit_variant", "lib.Shape.render"),
        ("lib.struct_variant", "lib.Shape.render"),
        ("lib.by_ref", "lib.Point.render"),
        ("lib.by_mut_ref", "lib.Pair.render"),
        ("lib.Point.new", "lib.Point.render"),
        ("lib.Point.copy_of", "lib.Point.render"),
        ("lib.Point.annotated", "lib.Point.render"),
        ("lib.turbofish_variant", "lib.Tree.render"),
        ("lib.Shape.self_variant", "lib.Shape.render"),
        ("lib.all_caps_tuple", "lib.ID.render"),
        ("lib.all_caps_variant", "lib.Value.render"),
    ],
    ids=[
        "unit-struct",
        "tuple-struct",
        "enum-tuple-variant",
        "enum-unit-variant",
        "enum-struct-variant",
        "reference",
        "mut-reference",
        "self-literal",
        "self-param",
        "self-annotation",
        "turbofish-enum-variant",
        "self-enum-variant",
        "all-caps-tuple-struct",
        "all-caps-enum-variant",
    ],
)
def test_a_constructed_local_types_its_method_call(
    calls: _Calls, caller: str, callee: str
) -> None:
    assert _callees(calls, caller) == {callee: "exact"}, calls


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("lib.brace", "lib.Point.render"),
        ("lib.assoc", "lib.Point.render"),
        ("lib.module_path", "shapes.Disk.area"),
        ("lib.short_name", "lib.S.render"),
        ("lib.lowercase_name", "lib.lower.render"),
        ("lib.turbofish_literal", "lib.Wrapper.render"),
    ],
    ids=[
        "struct-literal",
        "associated-fn",
        "module-qualified-struct",
        "one-letter-struct-literal",
        "lowercase-struct-literal",
        "turbofish-struct-literal",
    ],
)
def test_the_shapes_that_typed_before_still_do(
    calls: _Calls, caller: str, callee: str
) -> None:
    # Negatives: a struct literal, an associated call's return type and a
    # module-qualified struct literal keep their exact edges (and the
    # `Shape::Circle(1)` row above binds the enum, not the `Circle` struct).
    assert _callees(calls, caller).get(callee) == "exact", calls


@pytest.mark.parametrize(
    ("caller", "owner"),
    [
        ("lib.pascal_fn", "lib.Factory.render"),
        ("lib.Factory.self_pascal_fn", "lib.Factory.render"),
        ("lib.turbofish_pascal_fn", "lib.Wrapper.render"),
    ],
    ids=["type-path", "self-path", "turbofish-path"],
)
def test_a_pascal_case_associated_fn_types_its_local_by_its_return_type(
    calls: _Calls, caller: str, owner: str
) -> None:
    # Negative: `Factory::Build()` is spelled like the variant
    # `Shape::Circle(1)`, but it is a function, and its declared return type
    # says what `p` is, however the call spells its type (`Self::Build()`,
    # `Wrapper::<u8>::Build()`).
    callees = _callees(calls, caller)
    assert callees.get("lib.Product.render") == "exact", calls
    assert owner not in callees, calls


def test_a_self_associated_call_types_its_local_by_its_return_type(
    calls: _Calls,
) -> None:
    # `Self::make()` calls the impl's own `Point::make`, as `Point::make()`
    # does in `lib.assoc`.
    assert _callees(calls, "lib.Point.self_assoc") == {
        "lib.Point.make": "exact",
        "lib.Point.render": "exact",
    }, calls


def test_an_all_caps_fn_types_its_local_by_its_return_type(calls: _Calls) -> None:
    # Negative: a call spelled like the all-caps tuple struct `ID(1)` may be
    # a function, whose declared return type says what `p` is.
    callees = _callees(calls, "lib.all_caps_fn")
    assert callees.get("lib.Product.render") == "exact", calls


@pytest.mark.parametrize(
    "caller",
    ["paths.Disk.literal", "paths.Disk.param", "paths.Disk.annotated"],
    ids=["self-literal", "self-param", "self-annotation"],
)
def test_a_self_local_keeps_the_impl_targets_path(calls: _Calls, caller: str) -> None:
    # `Self` in `impl crate::shapes::Disk` is shapes::Disk; read as the bare
    # `Disk`, it resolved through the file's `use crate::rival::Disk;` and
    # bound rival's `area` exactly.
    assert _callees(calls, caller) == {"shapes.Disk.area": "exact"}, calls
    assert _callees(calls, "paths.rival") == {"rival.Disk.area": "exact"}, calls
