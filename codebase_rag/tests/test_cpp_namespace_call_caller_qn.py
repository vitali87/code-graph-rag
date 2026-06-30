from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)

# (H) A free function and an inline class method, both inside a namespace, each
# (H) calling a namespaced free function. The definition pass binds their nodes
# (H) WITH the enclosing namespace (qn `...ns.free_caller`, `...ns.K.method`), but
# (H) the call pass built the caller qn WITHOUT the namespace (`...free_caller`,
# (H) `...K.method`), so every such CALLS edge's source dangled (matched no node)
# (H) and the call was lost. On real namespaced C++ (e.g. all of leveldb, in
# (H) `namespace leveldb`) this silently dropped the bulk of cross-file calls. The
# (H) caller qn must include the enclosing namespace, matching the node.
CPP_SOURCE = """
namespace acme {

int callee(int x) { return x + 1; }

int free_caller(int a) { return callee(a); }

class K {
public:
    int method(int b) { return callee(b); }
};

}  // namespace acme
"""


def test_namespaced_callers_attribute_calls_to_namespaced_qn(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_ns_calls"
    project.mkdir()
    (project / "sample.cpp").write_text(CPP_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    free_qn = next(
        (
            q
            for q in get_qualified_names(get_nodes(mock_ingestor, "Function"))
            if q.endswith(".acme.free_caller")
        ),
        None,
    )
    method_qn = next(
        (
            q
            for q in get_qualified_names(get_nodes(mock_ingestor, "Method"))
            if q.endswith(".acme.K.method")
        ),
        None,
    )
    assert free_qn is not None, "no ns.free_caller Function node"
    assert method_qn is not None, "no ns.K.method Method node"

    calls = get_relationships(mock_ingestor, "CALLS")
    # (H) ensure_relationship_batch(from_spec, rel_type, to_spec): from_spec[2] is
    # (H) the caller qn, to_spec[2] the callee qn.
    callers_of_callee = {
        c.args[0][2] for c in calls if str(c.args[2][2]).endswith(".callee")
    }
    assert free_qn in callers_of_callee, (
        f"expected CALLS from {free_qn} to callee; got {sorted(callers_of_callee)}"
    )
    assert method_qn in callers_of_callee, (
        f"expected CALLS from {method_qn} to callee; got {sorted(callers_of_callee)}"
    )
