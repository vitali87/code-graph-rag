"""`RustResolver` and its path helpers: a `Type::f` call's owner path, which
written paths provably leave the repository, the type a name reaches, which
block-local items a call site sits outside of, which callers resolve
caller-dependently, and which modules an item's scope is visible to."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.call_site import CallSite
from codebase_rag.parsers.rust.resolution import (
    RustResolver,
    _drop_angle_groups,
    _rust_assoc_path,
    _rust_enclosing_scopes,
    _rust_module_sees,
    rust_caller,
)
from codebase_rag.tests.collaborator_support import edges, index
from codebase_rag.tests.rust_fixture import RUST_CRATE
from evals.recorded_graph import RecordingIngestor

_RS = cs.SupportedLanguage.RUST
_LIB = "rp.src.lib"
_CLASH = "rp.src.clash"
_BODY_LOCAL = f"{_CLASH}.tally@9"


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RustResolver:
    updater = index(temp_repo / "rp", mock_ingestor, RUST_CRATE)
    rust = updater.factory.call_processor._resolver.rust
    assert isinstance(rust, RustResolver)
    return rust


def _site(
    call_name: str,
    module_qn: str = _LIB,
    caller_qn: str = f"{_LIB}.run",
    language: cs.SupportedLanguage = _RS,
) -> CallSite:
    return CallSite(call_name, module_qn, {}, None, caller_qn, language, None, False)


@pytest.mark.parametrize(
    ("call_name", "expected"),
    [
        ("Circle::new", (["Circle"], "new")),
        ("Vec::<u8>::new", (["Vec"], "new")),
        ("HashMap::<K, Vec<V>>::with_capacity", (["HashMap"], "with_capacity")),
        ("crate::util::helper", (["crate", "util"], "helper")),
        # A method call, a qualified self type and a bare name name no owner.
        ("a.b::c", None),
        ("<T as Tr>::f", None),
        ("new", None),
    ],
)
def test_an_associated_call_splits_into_owner_path_and_item(
    call_name: str, expected: tuple[list[str], str] | None
) -> None:
    assert _rust_assoc_path(_site(call_name)) == expected


def test_only_a_rust_site_has_an_associated_path_or_a_rust_caller() -> None:
    assert _rust_assoc_path(_site("A::b", language=cs.SupportedLanguage.PYTHON)) is None
    assert rust_caller(_RS, "q") == "q"
    assert rust_caller(cs.SupportedLanguage.GO, "q") is None


def test_angle_groups_drop_out_nested_and_all() -> None:
    assert _drop_angle_groups("a<b<c>>::d") == "a::d"
    assert _drop_angle_groups("plain::path") == "plain::path"


def test_a_callers_enclosing_scopes_run_innermost_first() -> None:
    assert _rust_enclosing_scopes(_LIB, f"{_LIB}.util.inner.f") == [
        f"{_LIB}.util.inner",
        f"{_LIB}.util",
    ]
    assert _rust_enclosing_scopes(_LIB, f"{_LIB}.run") == []
    assert _rust_enclosing_scopes(_LIB, "other.f") == []
    assert _rust_enclosing_scopes(_LIB, None) == []


def test_a_crate_entrys_items_are_seen_beside_it_but_not_by_another_crate() -> None:
    assert _rust_module_sees("rp.src.lib.util", _LIB)
    # lib.rs is the parent of the modules beside it.
    assert _rust_module_sees("rp.src.shapes", _LIB)
    # main.rs and src/bin/ are crates of their own.
    assert not _rust_module_sees("rp.src.main", _LIB)
    assert not _rust_module_sees("rp.src.bin.tool", _LIB)
    assert not _rust_module_sees("rp.src.shapes", _CLASH)


def test_only_a_toolchain_or_manifest_head_proves_a_path_external(
    resolver: RustResolver,
) -> None:
    assert resolver.rust_path_is_external("std::io", _LIB)
    assert resolver.rust_path_is_external("serde::Serialize", _LIB)
    # Through the file's `use std::fmt;`.
    assert resolver.rust_path_is_external("fmt::Write", "rp.src.shapes")
    assert not resolver.rust_path_is_external("crate::util", _LIB)
    # A head the manifest does not name may still be first-party.
    assert not resolver.rust_path_is_external("rand::Rng", _LIB)
    assert resolver.rust_file_use_is_external("fmt", "rp.src.shapes")
    assert not resolver.rust_file_use_is_external("helper", _LIB)
    assert not resolver.rust_file_use_is_external("unbound", _LIB)


def test_a_type_name_reaches_its_declaration(resolver: RustResolver) -> None:
    assert resolver.resolve_rust_class_qn("Circle", _LIB) == "rp.src.shapes.Circle"
    assert resolver.rust_reachable_type("Circle", _LIB) == "rp.src.shapes.Circle"
    assert resolver.rust_reachable_type("Square", _LIB) == f"{_CLASH}.Square"
    assert resolver.rust_reachable_type("Nowhere", _LIB) is None


def test_a_block_local_item_is_hidden_outside_its_block(
    resolver: RustResolver,
) -> None:
    # `outer`'s body spans bytes 108 to 216 of clash.rs.
    assert not resolver.rust_block_item_hidden(_BODY_LOCAL, _CLASH, 150)
    assert resolver.rust_block_item_hidden(_BODY_LOCAL, _CLASH, 250)
    # An unplaceable site keeps it reachable, and a module item is never hidden.
    assert not resolver.rust_block_item_hidden(_BODY_LOCAL, _CLASH, None)
    assert not resolver.rust_block_item_hidden(f"{_CLASH}.tally", _CLASH, 250)


def test_a_scoped_caller_resolves_caller_dependently(
    resolver: RustResolver,
) -> None:
    inner = "rp.src.shapes.inner"
    assert resolver.rust_caller_dependent(_site("f", inner, f"{inner}.deep")) is False
    # An inline mod's caller, and a file with block-local items.
    assert resolver.rust_caller_dependent(_site("f", _LIB, f"{_LIB}.util.helper"))
    assert resolver.rust_caller_dependent(_site("f", _CLASH, f"{_CLASH}.outer"))
    python = _site("f", inner, f"{inner}.deep", cs.SupportedLanguage.PYTHON)
    assert resolver.rust_caller_dependent(python) is False


def test_a_super_path_call_climbs_to_the_crate_root(
    resolver: RustResolver, mock_ingestor: RecordingIngestor
) -> None:
    calls = edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert {t for s, t in calls if s == "rp.src.shapes.inner.deep"} == {
        "rp.src.lib.util.helper"
    }
