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


def _index(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    name: str,
    lib_rs: str,
    modules: dict[str, str] | None = None,
    dependencies: str = "",
) -> str:
    project = temp_repo / name
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        _CARGO.format(name=name) + dependencies, encoding="utf-8"
    )
    (project / "src" / "lib.rs").write_text(_STRATEGIES + lib_rs, encoding="utf-8")
    for module, source in (modules or {}).items():
        (project / "src" / f"{module}.rs").write_text(source, encoding="utf-8")
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


_OWN_LITERAL = """\
pub struct LiteralStrategy;
impl LiteralStrategy { pub fn hit(&self) -> bool { true } }
"""


def test_payload_path_is_read_where_the_enum_declares_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: the payload lost its module path, so `a::` and `b::`
    # LiteralStrategy both became the caller's own LiteralStrategy.
    source = """\
pub mod a;
pub mod b;

pub enum Job {
    Literal(a::LiteralStrategy),
    Prefix(b::LiteralStrategy),
}

fn run(job: Job) -> bool {
    match job {
        Job::Literal(s) => s.hit(),
        Job::Prefix(s) => s.hit(),
    }
}
"""
    modules = {"a": _OWN_LITERAL, "b": _OWN_LITERAL}
    base = _index(temp_repo, mock_ingestor, "rs_match_paths", source, modules)
    package = base.removesuffix(".lib")
    edges = _hit_edges(mock_ingestor, f"{base}.run")
    assert edges == {
        _line_of(source, "        Job::Literal(s) => s.hit(),"): (
            f"{package}.a.LiteralStrategy.hit",
            _EXACT,
        ),
        _line_of(source, "        Job::Prefix(s) => s.hit(),"): (
            f"{package}.b.LiteralStrategy.hit",
            _EXACT,
        ),
    }, edges


def test_imported_enum_payload_is_read_in_the_enums_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: a bare payload name means the type the enum's own
    # module has under it, not the caller's LiteralStrategy.
    source = """\
pub mod strategies;
use crate::strategies::Strategy;

fn run(strategy: Strategy) -> bool {
    match strategy {
        Strategy::Literal(s) => s.hit(),
    }
}
"""
    strategies = (
        _OWN_LITERAL + "pub enum Strategy {\n    Literal(LiteralStrategy),\n}\n"
    )
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_match_imported",
        source,
        {"strategies": strategies},
    )
    package = base.removesuffix(".lib")
    edges = _hit_edges(mock_ingestor, f"{base}.run")
    assert list(edges.values()) == [
        (f"{package}.strategies.LiteralStrategy.hit", _EXACT)
    ], edges


def test_bare_variant_takes_the_enum_it_is_imported_from(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: a bare `Literal(s)` took any enum of the caller's
    # module declaring a `Literal`, here the unrelated `Local`, not the
    # `Command` the module imports the variant from. A function's own `use`
    # outranks the module's.
    source = """\
pub mod commands;
use crate::commands::Command::Literal;

pub enum Local {
    Literal(PrefixStrategy),
}

fn run(command: commands::Command) -> bool {
    match command {
        Literal(s) => s.hit(),
    }
}

fn scoped(local: Local) -> bool {
    use self::Local::*;
    match local {
        Literal(p) => p.hit(),
    }
}
"""
    commands = "pub enum Command {\n    Literal(crate::LiteralStrategy),\n}\n"
    base = _index(
        temp_repo, mock_ingestor, "rs_match_bare", source, {"commands": commands}
    )
    run = _hit_edges(mock_ingestor, f"{base}.run")
    assert list(run.values()) == [(f"{base}.LiteralStrategy.hit", _EXACT)], run
    scoped = _hit_edges(mock_ingestor, f"{base}.scoped")
    assert list(scoped.values()) == [(f"{base}.PrefixStrategy.hit", _EXACT)], scoped


def test_external_enum_does_not_take_a_first_party_namesakes_payload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: `external_crate::Strategy` matched no first-party
    # path, so the lookup fell back to the first-party `Strategy` by its
    # name alone and bound its payload, an exact edge into this crate. The
    # fixture was checked against a local stub of `external_crate`.
    source = """\
pub mod local;
use external_crate::Strategy;

fn written(strategy: external_crate::Strategy) -> bool {
    match strategy {
        external_crate::Strategy::Literal(s) => s.hit(),
        _ => false,
    }
}

fn imported(strategy: Strategy) -> bool {
    match strategy {
        Strategy::Literal(s) => s.hit(),
        _ => false,
    }
}
"""
    local = "pub enum Strategy {\n    Literal(crate::LiteralStrategy),\n}\n"
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_match_external",
        source,
        {"local": local},
        dependencies='\n[dependencies]\nexternal_crate = "1.0"\n',
    )
    for caller in ("written", "imported"):
        edges = _hit_edges(mock_ingestor, f"{base}.{caller}")
        assert (f"{base}.LiteralStrategy.hit", _EXACT) not in edges.values(), (
            caller,
            edges,
        )
