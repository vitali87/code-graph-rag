"""A Rust match-arm binding takes the variant's declared payload type.

`Strategy::Literal(s)` binds `s: LiteralStrategy`, the type the enum
declares for the variant. The binding was typed by the variant's NAME, which
is only right for the newtype idiom (`Command::Get(Get)`); for an
enum-of-strategies every arm was untyped, and `ref s` / `ref mut s` / `&s`
bound nothing at all, so `s.hit()` fell to a name-only match on whichever
`hit` sorted first (issue #2923: 22 globset strategy methods in ripgrep
reported dead). The fixtures pass `cargo check`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'

_STRATEGIES = """\
pub struct LiteralStrategy;
impl LiteralStrategy { pub fn hit(&self) -> bool { true } }
pub struct PrefixStrategy;
impl PrefixStrategy { pub fn hit(&self) -> bool { false } }
pub struct Exact;
impl Exact { pub fn hit(&self) -> bool { true } }
"""


def _index(temp_repo: Path, mock_ingestor: MagicMock, name: str, lib_rs: str) -> str:
    project = temp_repo / name
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(_CARGO.format(name=name), encoding="utf-8")
    (project / "src" / "lib.rs").write_text(_STRATEGIES + lib_rs, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    return f"{name}.src.lib"


def _hit_edges(mock_ingestor: MagicMock, caller: str) -> dict[int, tuple[str, str]]:
    # {call line: (callee, resolution)} for the caller's `.hit()` calls.
    edges: dict[int, tuple[str, str]] = {}
    for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value):
        src, dst = str(call.args[0][2]), str(call.args[2][2])
        if src != caller or not dst.endswith(".hit"):
            continue
        props = call.kwargs.get("properties") or (
            call.args[3] if len(call.args) > 3 else {}
        )
        props = props or {}
        line = props.get(cs.KEY_LINE)
        assert isinstance(line, int), props
        edges[line] = (dst, str(props.get(cs.KEY_RESOLUTION)))
    return edges


def _line_of(source: str, needle: str) -> int:
    return (_STRATEGIES + source).splitlines().index(needle) + 1


_EXACT = cs.EdgeResolution.EXACT


def test_ref_bindings_take_each_variants_payload_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's reproduction.
    source = """\
enum Strategy {
    Literal(LiteralStrategy),
    Prefix(PrefixStrategy),
}

impl Strategy {
    fn check(&self) -> bool {
        match *self {
            Strategy::Literal(ref s) => s.hit(),
            Strategy::Prefix(ref s) => s.hit(),
        }
    }
}
"""
    base = _index(temp_repo, mock_ingestor, "rs_match_ref", source)
    edges = _hit_edges(mock_ingestor, f"{base}.Strategy.check")
    assert edges == {
        _line_of(source, "            Strategy::Literal(ref s) => s.hit(),"): (
            f"{base}.LiteralStrategy.hit",
            _EXACT,
        ),
        _line_of(source, "            Strategy::Prefix(ref s) => s.hit(),"): (
            f"{base}.PrefixStrategy.hit",
            _EXACT,
        ),
    }, edges


def test_value_mut_and_reference_bindings_through_self_paths(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = """\
enum Strategy {
    Literal(LiteralStrategy),
    Prefix(PrefixStrategy),
    Exact(Exact),
}

impl Strategy {
    fn by_value(self) -> bool {
        match self {
            Self::Literal(s) => s.hit(),
            Self::Prefix(mut p) => p.hit(),
            Self::Exact(e) => e.hit(),
        }
    }

    fn by_reference(&self) -> bool {
        match self {
            &Strategy::Literal(ref mut s) => s.hit(),
            Strategy::Prefix(p) => p.hit(),
            _ => false,
        }
    }
}

fn borrowed(strategies: &[Strategy]) -> bool {
    match &strategies[0] {
        Strategy::Literal(s) => s.hit(),
        _ => false,
    }
}
"""
    base = _index(temp_repo, mock_ingestor, "rs_match_self", source)
    by_value = _hit_edges(mock_ingestor, f"{base}.Strategy.by_value")
    assert sorted(by_value.values()) == [
        (f"{base}.Exact.hit", _EXACT),
        (f"{base}.LiteralStrategy.hit", _EXACT),
        (f"{base}.PrefixStrategy.hit", _EXACT),
    ], by_value
    by_reference = _hit_edges(mock_ingestor, f"{base}.Strategy.by_reference")
    assert sorted(by_reference.values()) == [
        (f"{base}.LiteralStrategy.hit", _EXACT),
        (f"{base}.PrefixStrategy.hit", _EXACT),
    ], by_reference
    borrowed = _hit_edges(mock_ingestor, f"{base}.borrowed")
    assert list(borrowed.values()) == [(f"{base}.LiteralStrategy.hit", _EXACT)], (
        borrowed
    )


def test_glob_imported_variant_resolves_through_its_enum(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = """\
pub enum Strategy {
    Literal(LiteralStrategy),
    Prefix(PrefixStrategy),
}

use self::Strategy::*;

fn bare(strategy: &Strategy) -> bool {
    match strategy {
        Literal(s) => s.hit(),
        Prefix(p) => p.hit(),
    }
}
"""
    base = _index(temp_repo, mock_ingestor, "rs_match_glob", source)
    edges = _hit_edges(mock_ingestor, f"{base}.bare")
    assert sorted(edges.values()) == [
        (f"{base}.LiteralStrategy.hit", _EXACT),
        (f"{base}.PrefixStrategy.hit", _EXACT),
    ], edges


def test_newtype_variant_still_binds_its_payload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the newtype idiom (variant named after its payload) kept
    # working on main by coincidence and must keep working on purpose.
    source = """\
enum Command {
    Exact(Exact),
}

fn run(cmd: Command) -> bool {
    match cmd {
        Command::Exact(e) => e.hit(),
    }
}
"""
    base = _index(temp_repo, mock_ingestor, "rs_match_newtype", source)
    edges = _hit_edges(mock_ingestor, f"{base}.run")
    assert list(edges.values()) == [(f"{base}.Exact.hit", _EXACT)], edges


def test_multi_field_variant_binds_no_payload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: only a single-field variant has one payload type to bind.
    source = """\
enum Pair {
    Both(LiteralStrategy, PrefixStrategy),
}

fn first(pair: Pair) -> bool {
    match pair {
        Pair::Both(a, _b) => a.hit(),
    }
}
"""
    base = _index(temp_repo, mock_ingestor, "rs_match_pair", source)
    edges = _hit_edges(mock_ingestor, f"{base}.first")
    assert (f"{base}.PrefixStrategy.hit", _EXACT) not in edges.values(), edges
