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
impl Shape { pub fn render(&self) -> u32 { 2 } }
impl Point {
    pub fn render(&self) -> u32 { self.x }
    pub fn new() -> Self { let p = Self { x: 0 }; p.render(); p }
    pub fn copy_of(&self, other: &Self) -> u32 { other.render() }
    pub fn annotated(&self) -> u32 { let s: Self = Point { x: 2 }; s.render() }
    pub fn make() -> Point { Point { x: 5 } }
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
"""
_SHAPES = """\
pub struct Disk { pub r: u32 }
impl Disk { pub fn area(&self) -> u32 { self.r } }
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
    ],
    ids=["struct-literal", "associated-fn", "module-qualified-struct"],
)
def test_the_shapes_that_typed_before_still_do(
    calls: _Calls, caller: str, callee: str
) -> None:
    # Negatives: a struct literal, an associated call's return type and a
    # module-qualified struct literal keep their exact edges (and the
    # `Shape::Circle(1)` row above binds the enum, not the `Circle` struct).
    assert _callees(calls, caller).get(callee) == "exact", calls
