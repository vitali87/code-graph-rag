# Singleton accessor chains (issue #896). The registrar pattern loses both
# onward edges: the chained hop off a pointer-returning static
# (`GetInstance()->GetWindowClass()`) and the `new Registrar()` construction
# inside the INLINE in-class method body, so the class's ctor/dtor and the
# chained member all report dead despite live callers.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

MAIN_CPP = """
class WindowClassRegistrar {
 public:
  ~WindowClassRegistrar() = default;
  static WindowClassRegistrar* GetInstance() {
    if (!instance_) {
      instance_ = new WindowClassRegistrar();
    }
    return instance_;
  }
  const wchar_t* GetWindowClass();

 private:
  WindowClassRegistrar() = default;
  static WindowClassRegistrar* instance_;
};

WindowClassRegistrar* WindowClassRegistrar::instance_ = nullptr;

const wchar_t* WindowClassRegistrar::GetWindowClass() {
  return L"CLASS";
}

int use() {
  const wchar_t* wc = WindowClassRegistrar::GetInstance()->GetWindowClass();
  return wc != nullptr;
}
"""


@pytest.fixture
def cpp_singleton_project(temp_repo: Path) -> Path:
    root = temp_repo / "singleton"
    root.mkdir()
    (root / "main.cpp").write_text(MAIN_CPP, encoding="utf-8")
    return root


def _rels(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel_type
    }


def _has(edges: set[tuple[str, str]], src: str, dst: str) -> bool:
    return any(s.endswith(src) and d.endswith(dst) for s, d in edges)


def test_static_accessor_call_resolves(
    cpp_singleton_project: Path, mock_ingestor: MagicMock
):
    # The already-working half: `use` calls the static accessor.
    run_updater(cpp_singleton_project, mock_ingestor)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(calls, ".use", ".WindowClassRegistrar.GetInstance"), sorted(calls)


def test_chained_hop_off_pointer_returning_static(
    cpp_singleton_project: Path, mock_ingestor: MagicMock
):
    # `GetInstance()` returns `WindowClassRegistrar*`; the chained
    # `->GetWindowClass()` must map through the POINTER return type to the
    # class and land on the member.
    run_updater(cpp_singleton_project, mock_ingestor)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(calls, ".use", ".WindowClassRegistrar.GetWindowClass"), sorted(calls)


def test_new_in_inline_method_emits_ctor_call(
    cpp_singleton_project: Path, mock_ingestor: MagicMock
):
    # `new WindowClassRegistrar()` inside the INLINE in-class body of
    # GetInstance must call the (private, defaulted) constructor.
    run_updater(cpp_singleton_project, mock_ingestor)
    calls = _rels(mock_ingestor, cs.RelationshipType.CALLS.value)
    assert _has(
        calls,
        ".WindowClassRegistrar.GetInstance",
        ".WindowClassRegistrar.WindowClassRegistrar",
    ), sorted(calls)


def test_new_in_inline_method_emits_instantiates(
    cpp_singleton_project: Path, mock_ingestor: MagicMock
):
    run_updater(cpp_singleton_project, mock_ingestor)
    inst = _rels(mock_ingestor, cs.RelationshipType.INSTANTIATES.value)
    assert _has(
        inst, ".WindowClassRegistrar.GetInstance", ".WindowClassRegistrar"
    ), sorted(inst)
