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

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from evals.cgr_graph import _StatefulIngestor

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
        path = project / "src" / f"{module}.rs"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
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


def test_self_and_super_paths_read_the_callers_own_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: `self::Strategy` matched by name alone could take
    # module `a`'s Strategy in module `b`, binding `a`'s payload exactly.
    # `super::` counts from the inline `mod` the caller sits in.
    module_a = """\
pub struct AStrat;
impl AStrat { pub fn hit(&self) -> bool { true } }
pub enum Strategy {
    Literal(AStrat),
}
"""
    module_b = """\
pub struct BStrat;
impl BStrat { pub fn hit(&self) -> bool { false } }
pub enum Strategy {
    Literal(BStrat),
}

pub fn run(strategy: Strategy) -> bool {
    match strategy {
        self::Strategy::Literal(s) => s.hit(),
    }
}

pub mod inner {
    pub fn run(strategy: super::Strategy) -> bool {
        match strategy {
            super::Strategy::Literal(s) => s.hit(),
        }
    }
}
"""
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_match_relative",
        "pub mod a;\npub mod b;\n",
        {"a": module_a, "b": module_b},
    )
    package = base.removesuffix(".lib")
    for caller in ("b.run", "b.inner.run"):
        edges = _hit_edges(mock_ingestor, f"{package}.{caller}")
        assert list(edges.values()) == [(f"{package}.b.BStrat.hit", _EXACT)], (
            caller,
            edges,
        )


def _strategy_module(strat: str) -> str:
    return (
        f"pub struct {strat};\n"
        f"impl {strat} {{ pub fn hit(&self) -> bool {{ true }} }}\n"
        f"pub enum Strategy {{\n    Literal({strat}),\n}}\n"
    )


def test_a_function_local_use_of_the_enum_shadows_the_files(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: `Strategy::Literal(s)` read the file's `use` of
    # `left::Strategy` though the function's own `use` binds `right`'s.
    source = """\
pub mod left;
pub mod right;
use crate::left::Strategy;

fn run(strategy: right::Strategy) -> bool {
    use crate::right::Strategy;
    match strategy {
        Strategy::Literal(s) => s.hit(),
    }
}

fn other(strategy: Strategy) -> bool {
    match strategy {
        Strategy::Literal(s) => s.hit(),
    }
}
"""
    modules = {"left": _strategy_module("LStrat"), "right": _strategy_module("RStrat")}
    base = _index(temp_repo, mock_ingestor, "rs_match_local_use", source, modules)
    package = base.removesuffix(".lib")
    run = _hit_edges(mock_ingestor, f"{base}.run")
    assert list(run.values()) == [(f"{package}.right.RStrat.hit", _EXACT)], run
    other = _hit_edges(mock_ingestor, f"{base}.other")
    assert list(other.values()) == [(f"{package}.left.LStrat.hit", _EXACT)], other


def test_a_relative_payload_path_is_the_enum_modules_child(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: `V(a::Handler)` in `right` is `right::a::Handler`,
    # never `left::a::Handler` matched by its suffix.
    handler = (
        "pub struct Handler;\nimpl Handler { pub fn hit(&self) -> bool { true } }\n"
    )
    right = """\
pub mod a;

pub enum Job {
    V(a::Handler),
}

pub fn run(job: Job) -> bool {
    match job {
        Job::V(x) => x.hit(),
    }
}
"""
    modules = {
        "left": "pub mod a;\n",
        "left/a": handler,
        "right": right,
        "right/a": handler,
    }
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_match_relative_payload",
        "pub mod left;\npub mod right;\n",
        modules,
    )
    package = base.removesuffix(".lib")
    edges = _hit_edges(mock_ingestor, f"{package}.right.run")
    assert list(edges.values()) == [(f"{package}.right.a.Handler.hit", _EXACT)], edges


def test_a_used_reexport_of_the_enum_is_followed(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: a function's `use crate::facade::Strategy` names a
    # re-export, followed to the enum `inner` declares.
    source = """\
pub mod facade;
pub mod inner;

fn run(strategy: facade::Strategy) -> bool {
    use crate::facade::Strategy;
    match strategy {
        Strategy::Literal(s) => s.hit(),
    }
}
"""
    modules = {
        "inner": _strategy_module("IStrat"),
        "facade": "pub use crate::inner::Strategy;\n",
    }
    base = _index(temp_repo, mock_ingestor, "rs_match_reexport", source, modules)
    package = base.removesuffix(".lib")
    edges = _hit_edges(mock_ingestor, f"{base}.run")
    assert list(edges.values()) == [(f"{package}.inner.IStrat.hit", _EXACT)], edges


def test_an_inline_modules_own_enum_shadows_the_files_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: inside `mod inner`, its own `Strategy` is meant, not
    # the file's `use crate::left::Strategy`.
    source = """\
pub mod left;
use crate::left::Strategy;

pub mod inner {
    pub struct OwnStrat;
    impl OwnStrat { pub fn hit(&self) -> bool { false } }
    pub enum Strategy {
        Literal(OwnStrat),
    }

    pub fn run(strategy: Strategy) -> bool {
        match strategy {
            Strategy::Literal(s) => s.hit(),
        }
    }
}
"""
    modules = {"left": _strategy_module("LStrat")}
    base = _index(temp_repo, mock_ingestor, "rs_match_inline_own", source, modules)
    edges = _hit_edges(mock_ingestor, f"{base}.inner.run")
    assert list(edges.values()) == [(f"{base}.inner.OwnStrat.hit", _EXACT)], edges


def test_an_imported_external_payload_head_takes_no_namesake(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # PR #3014 review: `use external_crate::a` binds `a`, so `V(a::Handler)`
    # is the external crate's, never a first-party `x::a::Handler`.
    handler = (
        "pub struct Handler;\nimpl Handler { pub fn hit(&self) -> bool { true } }\n"
    )
    source = """\
pub mod x;
use external_crate::a;

pub enum Job {
    V(a::Handler),
}

fn run(job: Job) -> bool {
    match job {
        Job::V(h) => h.hit(),
    }
}
"""
    base = _index(
        temp_repo,
        mock_ingestor,
        "rs_match_external_payload",
        source,
        {"x": "pub mod a;\n", "x/a": handler},
        dependencies='\n[dependencies]\nexternal_crate = "1.0"\n',
    )
    package = base.removesuffix(".lib")
    edges = _hit_edges(mock_ingestor, f"{base}.run")
    assert (f"{package}.x.a.Handler.hit", _EXACT) not in edges.values(), edges


_INCREMENTAL = {
    "Cargo.toml": _CARGO.format(name="rs_match_incremental"),
    "src/lib.rs": "pub mod strategies;\npub mod run;\n",
    "src/strategies.rs": _STRATEGIES
    + "pub enum Strategy {\n    Literal(LiteralStrategy),\n"
    + "    Prefix(PrefixStrategy),\n}\n",
    "src/run.rs": """\
use crate::strategies::Strategy;

pub fn check(strategy: Strategy) -> bool {
    match strategy {
        Strategy::Literal(s) => s.hit(),
        Strategy::Prefix(s) => s.hit(),
    }
}
""",
}


def _check_calls(store: _StatefulIngestor) -> dict[tuple[str, str, int], str]:
    calls: dict[tuple[str, str, int], str] = {}
    for edge in store.edges:
        if edge[2] != cs.RelationshipType.CALLS.value or not str(edge[1]).endswith(
            ".run.check"
        ):
            continue
        props = store.props_for(edge)
        line = props.get(cs.KEY_LINE)
        assert isinstance(line, int), props
        calls[(str(edge[1]), str(edge[4]), line)] = str(props.get(cs.KEY_RESOLUTION))
    return calls


def test_editing_only_the_caller_keeps_the_payload_types(tmp_path: Path) -> None:
    # PR #3014 review: only parsing filled the payload map, so a sync that
    # re-parses the caller but not the enum's unchanged file typed each arm
    # by its variant name, losing the calls a full build binds.
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("rust parser not available")
    root = tmp_path / "rs_match_incremental"
    for rel, text in _INCREMENTAL.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()

    def sync(force: bool) -> dict[tuple[str, str, int], str]:
        GraphUpdater(
            ingestor=store, repo_path=root, parsers=parsers, queries=queries
        ).run(force=force)
        return _check_calls(store)

    fresh = sync(force=True)
    assert sorted(callee.rsplit(".", 2)[-2] for _, callee, _ in fresh) == [
        "LiteralStrategy",
        "PrefixStrategy",
    ], fresh
    assert set(fresh.values()) == {_EXACT}, fresh

    run_rs = root / "src" / "run.rs"
    run_rs.write_text(run_rs.read_text() + "// unrelated edit\n", encoding="utf-8")

    assert sync(force=False) == fresh
