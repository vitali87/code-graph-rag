"""A Rust `impl Trait for &T` / `&mut T` / `*const T` block is on `T`.

The impl target's name was read from `type_identifier`, `generic_type`,
`primitive_type` and `scoped_type_identifier` only, so for `&'a mut Ser` it
found no name and the whole block was skipped: none of its methods became
nodes, the calls inside them vanished, the helpers only they call were
reported dead, and its associated types registered as module-level `Type`s.
That is the shape of serde's Serializer/Deserializer impls (issue #3175).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
)

_LIB = """\
pub trait Serializer {
    type Error;
    fn serialize_unit(self) -> u8;
}

pub struct Ser;

impl Ser {
    fn write_null(&mut self) -> u8 { 0 }
}

impl<'a> Serializer for &'a mut Ser {
    type Error = ();
    fn serialize_unit(self) -> u8 { self.write_null() }
}

pub fn run(s: &mut Ser) -> u8 { s.serialize_unit() }

pub struct List { items: Vec<u32> }

impl List {
    fn first(&self) -> u32 { self.items[0] }
}

impl<'a> IntoIterator for &'a List {
    type Item = &'a u32;
    type IntoIter = std::slice::Iter<'a, u32>;
    fn into_iter(self) -> Self::IntoIter { let _ = self.first(); self.items.iter() }
}

pub trait Shout { fn shout(&self) -> String; }

impl Shout for &str {
    fn shout(&self) -> String { loud(self) }
}

fn loud(s: &str) -> String { s.to_uppercase() }

pub struct Raw;

impl Shout for *const Raw {
    fn shout(&self) -> String { String::new() }
}

impl Shout for u8 {
    fn shout(&self) -> String { self.to_string() }
}

impl Shout for &[u8] {
    fn shout(&self) -> String { String::new() }
}

impl Shout for &dyn std::fmt::Debug {
    fn shout(&self) -> String { String::new() }
}
"""


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> MagicMock:
    root = tmp_path_factory.mktemp("rs3175") / "rsref"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "rsref"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (root / "src" / "lib.rs").write_text(_LIB, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="rust")
    return mock


def _short(qn: object) -> str:
    return str(qn).split(".src.lib.", 1)[-1]


def _qns(indexed: MagicMock, label: cs.NodeLabel) -> set[str]:
    return {_short(c.args[1][cs.KEY_QUALIFIED_NAME]) for c in get_nodes(indexed, label)}


def _rels(indexed: MagicMock, rel: cs.RelationshipType) -> dict[tuple[str, str], str]:
    return {
        (_short(c.args[0][2]), _short(c.args[2][2])): str(
            (c.kwargs.get("properties") or {}).get(cs.KEY_RESOLUTION)
        )
        for c in get_relationships(indexed, rel)
    }


def test_the_methods_belong_to_the_referenced_type(indexed: MagicMock) -> None:
    methods = _qns(indexed, cs.NodeLabel.METHOD)
    assert {
        "Ser.serialize_unit",
        "List.into_iter",
        "str.shout",
        "Raw.shout",
    } <= methods, methods


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("Ser.serialize_unit", "Ser.write_null"),
        ("List.into_iter", "List.first"),
        ("str.shout", "loud"),
        ("run", "Ser.serialize_unit"),
    ],
)
def test_their_calls_resolve_exact(
    indexed: MagicMock, caller: str, callee: str
) -> None:
    calls = _rels(indexed, cs.RelationshipType.CALLS)
    assert calls.get((caller, callee)) == "exact", calls


def test_the_impl_links_the_type_to_its_trait(indexed: MagicMock) -> None:
    assert ("Ser", "Serializer") in _rels(indexed, cs.RelationshipType.IMPLEMENTS)
    overrides = _rels(indexed, cs.RelationshipType.OVERRIDES)
    assert ("Ser.serialize_unit", "Serializer.serialize_unit") in overrides


def test_associated_types_are_the_types_own(indexed: MagicMock) -> None:
    types = _qns(indexed, cs.NodeLabel.TYPE)
    assert {"Ser.Error", "List.Item", "List.IntoIter"} <= types, types
    assert not {"Error", "Item", "IntoIter"} & types, types


def test_a_slice_or_dyn_reference_is_not_its_element_type(
    indexed: MagicMock,
) -> None:
    # Negatives: `&[u8]` names no type and `&dyn Debug` a trait object, so
    # neither block's `shout` joins `impl Shout for u8`'s.
    shouts = {qn for qn in _qns(indexed, cs.NodeLabel.METHOD) if "shout" in qn}
    assert "u8.shout" in shouts, shouts
    assert shouts <= {"Shout.shout", "str.shout", "Raw.shout", "u8.shout"}, shouts
