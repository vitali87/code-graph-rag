# Implicit C++ base ctor/dtor chains (issue #892). A derived constructor
# whose member initializer list does not name its base still runs the
# base's default constructor, and destruction runs base destructors after
# the derived one; neither has an AST node, so the base chain had zero
# incoming CALLS and reported dead (wonderous `Win32Window::Win32Window`).
# Registry guarded: an unresolvable (external) base emits nothing.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

MAIN_CPP = """
class Base {
 public:
  Base();
  ~Base();
};

class Derived : public Base {
 public:
  Derived(int v);
};

class Child : public Base {
 public:
  Child(int v);
  ~Child();
};

class External : public UnknownBase {
 public:
  External(int v);
};

Base::Base() {}
Base::~Base() {}
Derived::Derived(int v) {}
Child::Child(int v) : Base() {}
Child::~Child() {}
External::External(int v) {}

class GrandBase {
 public:
  GrandBase();
  ~GrandBase();
};

class Middle : public GrandBase {
 public:
  Middle(int v);
  ~Middle();
};

GrandBase::GrandBase() {}
GrandBase::~GrandBase() {}
Middle::Middle(int v) {}

int main() {
  int v = 1;
  Derived(1);
  Child c(v);
  External e(v);
  Middle(2);
  return 0;
}
"""


@pytest.fixture
def cpp_base_chain_project(temp_repo: Path) -> Path:
    root = temp_repo / "chain"
    root.mkdir()
    (root / "main.cpp").write_text(MAIN_CPP, encoding="utf-8")
    return root


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
    }


def _has(edges: set[tuple[str, str]], src: str, dst: str) -> bool:
    return any(s.endswith(src) and d.endswith(dst) for s, d in edges)


def test_implicit_base_ctor_is_called(
    cpp_base_chain_project: Path, mock_ingestor: MagicMock
):
    # Derived's ctor has NO member initializer list, yet running it runs
    # Base's default ctor.
    run_updater(cpp_base_chain_project, mock_ingestor)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".Derived.Derived", ".Base.Base"), sorted(calls)


def test_explicit_base_init_still_resolves(
    cpp_base_chain_project: Path, mock_ingestor: MagicMock
):
    # Child names the base explicitly (`: Base()`), covered by the
    # member-init pass; the implicit path must not regress or double it.
    # Raw call count (not the deduplicating set) proves single emission
    # (CodeRabbit round 3).
    run_updater(cpp_base_chain_project, mock_ingestor)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".Child.Child", ".Base.Base"), sorted(calls)
    raw = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
        and str(c.args[0][2]).endswith(".Child.Child")
        and str(c.args[2][2]).endswith(".Base.Base")
    ]
    assert len(raw) == 1, raw


def test_declared_dtor_calls_base_dtor(
    cpp_base_chain_project: Path, mock_ingestor: MagicMock
):
    # ~Child runs ~Base after its own body.
    run_updater(cpp_base_chain_project, mock_ingestor)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".Child.~Child", ".Base.~Base"), sorted(calls)


def test_undeclared_dtor_falls_through_to_base(
    cpp_base_chain_project: Path, mock_ingestor: MagicMock
):
    # Derived declares no dtor, so destroying the `Derived(1)` temporary
    # runs ~Base directly; the construction site owns that edge (the
    # destructor-target walk must fall through INHERITS to the nearest
    # declared base dtor).
    run_updater(cpp_base_chain_project, mock_ingestor)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".main.main", ".Base.~Base"), sorted(calls)


def test_declaration_only_dtor_does_not_sever_the_chain(
    cpp_base_chain_project: Path, mock_ingestor: MagicMock
):
    # ~Middle is declared but never defined in the parsed source, so no
    # caller pass runs for it and it cannot emit ~Middle -> ~GrandBase.
    # Destroying a Middle runs EVERY ancestor dtor unconditionally, so the
    # construction site carries the full chain (Greptile round 1).
    run_updater(cpp_base_chain_project, mock_ingestor)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".main.main", ".Middle.~Middle"), sorted(calls)
    assert _has(calls, ".main.main", ".GrandBase.~GrandBase"), sorted(calls)


def test_external_base_emits_nothing(
    cpp_base_chain_project: Path, mock_ingestor: MagicMock
):
    # UnknownBase is unregistered; the implicit path must stay silent.
    run_updater(cpp_base_chain_project, mock_ingestor)
    calls = _calls(mock_ingestor)
    assert not any(d.endswith(".UnknownBase.UnknownBase") for _, d in calls), sorted(
        calls
    )
