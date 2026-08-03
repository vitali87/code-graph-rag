# A method call on a receiver whose type is a generic type parameter never
# resolved through the parameter's trait bound: the call either vanished
# (fn params, where clauses) or fell through to the name-based fallback and
# fabricated an edge onto an unrelated type's inherent method (impl-generic
# struct fields). Rust dispatches such calls to the bound trait's method, so
# the edge must target the trait (ripgrep: all seventeen Matcher default
# methods reported dead; issue #1047).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_LIB_RS = """\
pub trait Matcher {
    fn find(&self, hay: &str) -> bool;

    fn is_match(&self, hay: &str) -> bool {
        self.find(hay)
    }
}

pub fn search<M: Matcher>(m: M) -> bool {
    m.is_match("x")
}

pub struct Core<M> {
    matcher: M,
}

impl<M: Matcher> Core<M> {
    pub fn run(&self) -> bool {
        self.matcher.is_match("y")
    }

    pub fn get(self) -> M {
        self.matcher
    }

    pub fn run_via_get(self) -> bool {
        let m = self.get();
        m.is_match("g")
    }
}

pub fn search_where<M>(m: M) -> bool
where
    M: Matcher,
{
    m.is_match("z")
}

pub fn search_multi<M: Matcher + Clone>(m: M) -> bool {
    m.is_match("v")
}

pub struct Decoy;

impl Decoy {
    pub fn is_match(&self, _hay: &str) -> bool {
        false
    }

    pub fn poll(&self) -> bool {
        true
    }
}

pub fn use_decoy(d: Decoy) -> bool {
    d.is_match("w")
}

pub fn drain<W: std::io::Write>(w: W) -> bool {
    w.poll()
}

pub struct Maker;

impl Maker {
    pub fn make<M>() -> M {
        unimplemented!()
    }
}

pub fn capture_guard<M: Matcher>(_m: M) -> bool {
    let x: M = Maker::make();
    x.find("k")
}

pub fn no_capture<M: Matcher>(_m: M) -> bool {
    let x = Maker::make();
    x.find("k")
}
"""


# Multi-module crate: a bound's spelling must resolve STRICTLY (import map,
# exact module path, or same-module definition); a bare spelling naming an
# external trait (`use std::io::Write` then `W: Write`, prelude `Clone`) must
# not fuzzy-bind to an unrelated first-party type sharing the leaf name.
_MOD_FILES = {
    "src/lib.rs": "pub mod dom;\npub mod fmtx;\npub mod m;\npub mod sink;\n"
    "pub mod u;\npub mod u2;\npub mod util;\n",
    "src/fmtx.rs": """\
pub struct Write;

impl Write {
    pub fn flush(&self) -> bool {
        true
    }
}
""",
    "src/sink.rs": """\
use std::io::Write;

pub fn drain<W: Write>(w: W) -> bool {
    w.flush()
}
""",
    "src/dom.rs": """\
pub struct Clone;

impl Clone {
    pub fn poll(&self) -> bool {
        true
    }
}
""",
    "src/util.rs": """\
pub fn dup<T: Clone>(t: T) -> bool {
    t.poll()
}
""",
    "src/m.rs": """\
pub trait Matcher {
    fn find(&self, hay: &str) -> bool;

    fn is_match(&self, hay: &str) -> bool {
        self.find(hay)
    }
}
""",
    "src/u.rs": """\
pub fn go<M: crate::m::Matcher>(m: M) -> bool {
    m.is_match("q")
}
""",
    "src/u2.rs": """\
use crate::m::Matcher;

pub fn go2<M: Matcher>(m: M) -> bool {
    m.is_match("r")
}
""",
}


def _write(project: Path, files: dict[str, str]) -> None:
    for rel_path, source in files.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoding="utf-8", data=source)


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2])
        for call in get_relationships(mock_ingestor, RelationshipType.CALLS.value)
    }


def _build(temp_repo: Path, mock_ingestor: MagicMock, name: str) -> str:
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": f'[package]\nname = "{name}"\nversion = "0.1.0"\n',
            "src/lib.rs": _LIB_RS,
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    return f"{name}.src.lib"


def test_fn_generic_param_bound_dispatch(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _build(temp_repo, mock_ingestor, "rs_bound_fn")
    calls = _calls(mock_ingestor)
    assert (f"{base}.search", f"{base}.Matcher.is_match") in calls, calls


def test_where_clause_bound_dispatch(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    base = _build(temp_repo, mock_ingestor, "rs_bound_where")
    calls = _calls(mock_ingestor)
    assert (f"{base}.search_where", f"{base}.Matcher.is_match") in calls, calls


def test_multi_bound_resolves_through_first_party_trait(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _build(temp_repo, mock_ingestor, "rs_bound_multi")
    calls = _calls(mock_ingestor)
    assert (f"{base}.search_multi", f"{base}.Matcher.is_match") in calls, calls


def test_impl_generic_field_bound_dispatch_beats_decoy(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _build(temp_repo, mock_ingestor, "rs_bound_field")
    calls = _calls(mock_ingestor)
    assert (f"{base}.Core.run", f"{base}.Matcher.is_match") in calls, calls
    assert (f"{base}.Core.run", f"{base}.Decoy.is_match") not in calls, calls


def test_direct_inherent_call_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    base = _build(temp_repo, mock_ingestor, "rs_bound_decoy")
    calls = _calls(mock_ingestor)
    assert (f"{base}.use_decoy", f"{base}.Decoy.is_match") in calls, calls


def test_annotated_local_substitutes_caller_scope_bound(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `let x: M = ...` spells the CALLER's generic parameter, so the bound
    # substitution applies to the local exactly as to a parameter.
    base = _build(temp_repo, mock_ingestor, "rs_bound_let")
    calls = _calls(mock_ingestor)
    assert (f"{base}.capture_guard", f"{base}.Matcher.find") in calls, calls


def test_callee_return_generic_is_not_captured_by_caller_bounds(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `let x = Maker::make()` types x from the callee's declared return `M`,
    # which is MAKER's generic parameter; the caller's own `M: Matcher` bound
    # must not capture it and fabricate a Matcher edge.
    base = _build(temp_repo, mock_ingestor, "rs_bound_capture")
    calls = _calls(mock_ingestor)
    assert (f"{base}.no_capture", f"{base}.Matcher.find") not in calls, calls


def _build_modules(temp_repo: Path, mock_ingestor: MagicMock, name: str) -> str:
    project = temp_repo / name
    files = {"Cargo.toml": f'[package]\nname = "{name}"\nversion = "0.1.0"\n'}
    files.update(_MOD_FILES)
    _write(project, files)
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    return f"{name}.src"


def test_bare_imported_external_bound_does_not_substitute(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use std::io::Write` then `W: Write`: the bound names the EXTERNAL
    # trait, so the unrelated first-party fmtx::Write must not gain an edge.
    base = _build_modules(temp_repo, mock_ingestor, "rs_bound_use_ext")
    calls = _calls(mock_ingestor)
    assert (f"{base}.sink.drain", f"{base}.fmtx.Write.flush") not in calls, calls


def test_prelude_bound_does_not_substitute(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `T: Clone` with no import names the prelude trait; the first-party
    # dom::Clone struct sharing the leaf name must not gain an edge.
    base = _build_modules(temp_repo, mock_ingestor, "rs_bound_prelude")
    calls = _calls(mock_ingestor)
    assert (f"{base}.util.dup", f"{base}.dom.Clone.poll") not in calls, calls


def test_scoped_first_party_bound_dispatches(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `M: crate::m::Matcher` resolves by exact module path, so the scoped
    # spelling dispatches exactly as the bare one.
    base = _build_modules(temp_repo, mock_ingestor, "rs_bound_scoped")
    calls = _calls(mock_ingestor)
    assert (f"{base}.u.go", f"{base}.m.Matcher.is_match") in calls, calls


def test_cross_module_imported_bound_dispatches(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::m::Matcher` then `M: Matcher`: the import map carries the
    # already-resolved project qn, which substitutes directly.
    base = _build_modules(temp_repo, mock_ingestor, "rs_bound_use_fp")
    calls = _calls(mock_ingestor)
    assert (f"{base}.u2.go2", f"{base}.m.Matcher.is_match") in calls, calls


def test_same_impl_accessor_generic_return_substitutes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `let m = self.get()` returns the caller's OWN impl-header generic
    # (a method cannot shadow it, E0403), so the bound applies to the local.
    base = _build(temp_repo, mock_ingestor, "rs_bound_self_get")
    calls = _calls(mock_ingestor)
    assert (f"{base}.Core.run_via_get", f"{base}.Matcher.is_match") in calls, calls


def test_external_bound_does_not_fabricate_inherent_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `W: std::io::Write` names no first-party trait, so `w.poll()` cannot be
    # a first-party call; the name-based fallback must not bind it to the
    # unrelated inherent Decoy.poll.
    base = _build(temp_repo, mock_ingestor, "rs_bound_external")
    calls = _calls(mock_ingestor)
    assert (f"{base}.drain", f"{base}.Decoy.poll") not in calls, calls
