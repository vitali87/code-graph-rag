from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)

# An out-of-line C++ method definition (`int Calculator::add(...) {...}` at
# namespace/file scope) calling a free function. cgr's definition pass binds
# the METHOD node to the class (qn `...Calculator.add`), but the call pass
# computed the caller qn as a module-rooted free function (`...calc.add`),
# so the CALLS edge's source dangled (matched no node). The caller of a call
# inside an out-of-line method body must be the method's own node qn.
CPP_SOURCE = """
class Calculator {
public:
    int add(int a, int b);
};

int helper_fn(int x) { return x + 1; }

int Calculator::add(int a, int b) {
    return helper_fn(a) + b;
}
"""


def test_out_of_class_method_call_attributed_to_method_qn(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_ooc_calls"
    project.mkdir()
    (project / "calc.cpp").write_text(CPP_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    method_qns = get_qualified_names(get_nodes(mock_ingestor, "Method"))
    add_qn = next((q for q in method_qns if q.endswith(".Calculator.add")), None)
    assert add_qn is not None, f"no Calculator.add Method node: {method_qns}"

    calls = get_relationships(mock_ingestor, "CALLS")
    # ensure_relationship_batch(from_spec, rel_type, to_spec): from_spec[2] is
    # the caller qn, to_spec[2] the callee qn.
    callers_of_helper = {
        c.args[0][2] for c in calls if "helper_fn" in str(c.args[2][2])
    }
    assert add_qn in callers_of_helper, (
        f"expected CALLS from {add_qn} to helper_fn; "
        f"got callers {sorted(callers_of_helper)}"
    )
