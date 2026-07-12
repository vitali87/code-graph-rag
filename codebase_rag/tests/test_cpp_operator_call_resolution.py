# (H) C++ operator calls: a user-defined overload must win, and a primitive
# (H) builtin operator must produce NO call edge. The old table mapped common
# (H) operators to synthetic `builtin.cpp.operator_*` qns unconditionally,
# (H) which both shadowed real overloads and emitted edges to nodes that never
# (H) exist, silently dropped by the database (issue #652: 1,681 on souffle).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

CPP = """
namespace math {

class Vec {
public:
    int x;
    Vec operator+(const Vec& other) const;
};

Vec Vec::operator+(const Vec& other) const {
    Vec r;
    r.x = x + other.x;
    return r;
}

int combine(Vec a, Vec b) {
    Vec c = a + b;
    return c.x && a.x;
}

}
"""


def test_user_defined_operator_overload_wins_and_builtins_emit_no_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "vec.cpp").write_text(CPP)
    run_updater(temp_repo, mock_ingestor)

    node_keys = {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }
    calls = get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
    operator_targets = [call for call in calls if "operator" in str(call.args[2][2])]
    assert operator_targets, calls
    for call in operator_targets:
        to_label, _, to_qn = call.args[2]
        assert not str(to_qn).startswith("builtin."), call.args
        assert (str(to_label), to_qn) in node_keys, call.args
