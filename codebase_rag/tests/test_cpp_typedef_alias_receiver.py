from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def _calls_from(mock_ingestor: MagicMock, caller_suffix: str) -> set[str]:
    return {
        str(c.args[2][2])
        for c in get_relationships(mock_ingestor, "CALLS")
        if str(c.args[0][2]).endswith(caller_suffix)
    }


# (H) A field's declared type can be a `typedef`/`using` alias of a first-party class
# (H) rather than the class name itself. Resolving `m1_.Lock()` requires mapping the
# (H) alias (`MutexAlias`/`MutexUsing`) to its underlying class (`Mutex`); without it
# (H) the receiver has a type the resolver cannot turn into a class, so the call is
# (H) dropped (the name-only trie fallback is skipped once a receiver type is known).
# (H) `Alpha.Lock` sorts before `Mutex.Lock`, so a name-only guess would pick the
# (H) wrong one -- only alias-resolved field typing binds the correct `Mutex.Lock`.
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

typedef Mutex MutexAlias;
using MutexUsing = Mutex;

class DB {
 public:
  void Run() {
    m1_.Lock();
    m2_.Lock();
  }
 private:
  MutexAlias m1_;
  MutexUsing m2_;
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
    # (H) Both the typedef- and using-aliased field calls must bind Mutex.Lock.
    mutex_hits = [c for c in callees if c.endswith(".Mutex.Lock")]
    assert len(mutex_hits) >= 1, (
        f"m1_.Lock()/m2_.Lock() should resolve to Mutex.Lock via the alias, "
        f"got {callees}"
    )
    assert not any(c.endswith(".Alpha.Lock") for c in callees), (
        f"alias field calls must not resolve to the same-named Alpha.Lock, "
        f"got {callees}"
    )
