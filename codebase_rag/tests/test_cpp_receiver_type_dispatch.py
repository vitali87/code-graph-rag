from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    get_relationships,
    run_updater,
)

# (H) Two classes define a method of the same name; only the receiver's type tells
# (H) which one a call `z->run()` / `z.run()` targets. cgr resolved C++ member calls
# (H) by the bare method name (the field_expression yielded only `run`), so the
# (H) name-only trie fallback bound every `run()` call to whichever `run` sorted
# (H) first (`Alpha.run`), regardless of the receiver. With receiver type inference
# (H) (parameter/local var -> bare class name), `z` is a `Zeta`, so the call must
# (H) resolve to `Zeta.run`. `Alpha` sorts before `Zeta`, so the wrong (old) answer
# (H) is deterministic and this test is a real RED.
CPP_SOURCE = """
namespace ns {

class Alpha {
 public:
  int run() { return 1; }
};

class Zeta {
 public:
  int run() { return 2; }
};

int use_ptr(Zeta* z) { return z->run(); }

int use_val(Zeta z) { return z.run(); }

}  // namespace ns
"""


def _calls_to_run(mock_ingestor: MagicMock) -> dict[str, str]:
    # (H) map caller-qn -> callee-qn for every CALLS edge whose callee is a `run`.
    out: dict[str, str] = {}
    for c in get_relationships(mock_ingestor, "CALLS"):
        callee = str(c.args[2][2])
        if callee.rsplit(".", 1)[-1] == "run":
            out[str(c.args[0][2])] = callee
    return out


def test_cpp_member_call_resolves_via_receiver_type(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_recv"
    project.mkdir()
    (project / "s.cpp").write_text(CPP_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    calls = _calls_to_run(mock_ingestor)
    ptr_caller = next(q for q in calls if q.endswith(".use_ptr"))
    val_caller = next(q for q in calls if q.endswith(".use_val"))

    assert calls[ptr_caller].endswith(".Zeta.run"), (
        f"z->run() should resolve to Zeta.run, got {calls[ptr_caller]}"
    )
    assert calls[val_caller].endswith(".Zeta.run"), (
        f"z.run() should resolve to Zeta.run, got {calls[val_caller]}"
    )
