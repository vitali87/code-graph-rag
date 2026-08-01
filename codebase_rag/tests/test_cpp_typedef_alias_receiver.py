from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp import CppTypeInferenceEngine
from codebase_rag.tests.conftest import get_relationships, run_updater


def _root_node(source: str):
    parsers, _ = load_parsers()
    return parsers["cpp"].parse(source.encode("utf-8")).root_node


def _calls_from(mock_ingestor: MagicMock, caller_suffix: str) -> set[str]:
    return {
        str(c.args[2][2])
        for c in get_relationships(mock_ingestor, "CALLS")
        if str(c.args[0][2]).endswith(caller_suffix)
    }


# A field's declared type can be a `typedef`/`using` alias of a first-party class
# rather than the class name itself. Resolving `m_.Lock()` requires mapping the
# alias to its underlying class; without it the receiver has a type the resolver
# cannot turn into a class, so the call is dropped. The typedef field (`Mutex`) and
# the using field (`Gizmo`) target distinct methods, so BOTH alias forms are asserted
# independently. `Alpha.Lock` sorts before `Mutex.Lock`, so a name-only guess would
# pick the wrong one; only alias-resolved field typing binds Mutex.Lock.
_SOURCE = """
namespace ns {

class Alpha {
 public:
  void Lock() {}
};

class Mutex {
 public:
  void Lock() {}
};

class Gizmo {
 public:
  void Poke() {}
};

typedef Mutex MutexAlias;
using GizmoUsing = Gizmo;

class DB {
 public:
  void Run() {
    m_.Lock();
    g_.Poke();
  }
 private:
  MutexAlias m_;
  GizmoUsing g_;
};

}  // namespace ns
"""


def test_cpp_typedef_and_using_alias_field_calls_resolve_to_underlying_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cpp_alias_field"
    project.mkdir()
    (project / "s.cpp").write_text(_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    callees = _calls_from(mock_ingestor, ".DB.Run")
    # typedef path: m_.Lock() binds Mutex.Lock (not the same-named Alpha.Lock).
    assert any(c.endswith(".Mutex.Lock") for c in callees), (
        f"m_.Lock() should resolve to Mutex.Lock via the typedef alias, got {callees}"
    )
    assert not any(c.endswith(".Alpha.Lock") for c in callees), (
        f"typedef alias field call must not resolve to Alpha.Lock, got {callees}"
    )
    # using path: g_.Poke() binds Gizmo.Poke.
    assert any(c.endswith(".Gizmo.Poke") for c in callees), (
        f"g_.Poke() should resolve to Gizmo.Poke via the using alias, got {callees}"
    )


# Correctness guards for the global bare-name alias map (PR #568 review):
# (1) an alias defined inside a function body is local and must not be collected;
# (2) the same alias name mapping to different types in two namespaces/files is
# ambiguous and must be dropped (not first-write-wins), so those receivers fall
# back to name-only resolution instead of a confidently-wrong typed edge.
def test_collect_type_aliases_skips_function_bodies_and_drops_conflicts() -> None:
    src = """
namespace a { typedef Widget Handle; }
namespace b { typedef Gizmo Handle; }
typedef Mutex Lock;
using AliasT = Lock;
void f() {
  typedef Local L;
  using Inner = Other;
}
"""
    aliases: dict[str, str] = {}
    conflicts: set[str] = set()
    CppTypeInferenceEngine().collect_type_aliases(_root_node(src), aliases, conflicts)

    # File-scope aliases resolve; the conflicting `Handle` is dropped, and the
    # function-local `L`/`Inner` are never collected.
    assert aliases == {"Lock": "Mutex", "AliasT": "Lock"}, aliases
    assert "Handle" in conflicts
    assert "L" not in aliases and "Inner" not in aliases
