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
"""


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


def test_external_bound_does_not_fabricate_inherent_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `W: std::io::Write` names no first-party trait, so `w.poll()` cannot be
    # a first-party call; the name-based fallback must not bind it to the
    # unrelated inherent Decoy.poll.
    base = _build(temp_repo, mock_ingestor, "rs_bound_external")
    calls = _calls(mock_ingestor)
    assert (f"{base}.drain", f"{base}.Decoy.poll") not in calls, calls
