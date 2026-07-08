# (H) The C++ function query captures a templated free function twice: the
# (H) template_declaration wrapper AND its inner function_definition. Both used
# (H) to register, so every template function minted a `qn@line` duplicate node
# (H) and Pass-3 caller attribution could bind the body's calls to the
# (H) duplicate instead of the natural qn (issue #652). The wrapper is the
# (H) canonical node (mirroring the class rule); the inner definition is
# (H) redundant and must not register.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

SAX_HDR = """
struct Aaa {
    bool start_object(int n) { return true; }
};
template<typename SAX>
bool run_parse(SAX* sax) {
    return sax->start_object(1);
}
"""


def test_template_function_registers_one_node_and_owns_its_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "sax.h").write_text(SAX_HDR)
    run_updater(temp_repo, mock_ingestor)

    function_qns = [
        c.args[1]["qualified_name"]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.FUNCTION.value
        and "run_parse" in c.args[1]["qualified_name"]
    ]
    project = temp_repo.name
    assert function_qns == [f"{project}.sax.run_parse"], function_qns

    start_object_calls = {
        call.args[0][2]
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if str(call.args[2][2]).endswith(".start_object")
    }
    assert start_object_calls == {f"{project}.sax.run_parse"}, start_object_calls
