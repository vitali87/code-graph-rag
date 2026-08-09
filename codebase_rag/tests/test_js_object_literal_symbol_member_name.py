"""An object-literal member keyed by a computed symbol must register with its
full bracketed name, not a dot-mangled fragment (issue #998)."""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships

SOURCE = """
const obj = {
  [Symbol.iterator] () { return helper() }
}
function helper () { return 1 }
module.exports = { obj }
"""


def _function_nodes(mock_ingestor: MagicMock) -> dict[str, str]:
    return {
        c.args[1]["qualified_name"]: c.args[1].get("name")
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == "Function"
    }


def test_computed_symbol_member_keeps_its_bracketed_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "obj.js").write_text(SOURCE, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project = temp_repo.name
    functions = _function_nodes(mock_ingestor)
    member_qn = f"{project}.obj.[Symbol.iterator]"
    assert member_qn in functions, functions
    assert functions[member_qn] == "[Symbol.iterator]", functions[member_qn]
    assert "iterator]" not in functions.values()

    calls = get_relationships(mock_ingestor, "CALLS")
    helper_qn = f"{project}.obj.helper"
    assert any(
        call.args[0][2] == member_qn and call.args[2][2] == helper_qn for call in calls
    ), [c.args for c in calls]
