# (H) TS declaration merging (`class Calculator` + `namespace Calculator`)
# (H) registers the namespace under a `qn@line` duplicate, and its members
# (H) under `Calculator@line.create`. Pass-3 caller attribution re-derived the
# (H) natural `Calculator.create` from the AST, so every edge FROM a merged
# (H) namespace member had a phantom endpoint; and variant fan-out emitted the
# (H) namespace duplicate with the CALLEE's label (Function Logger@13 for a
# (H) Class node), a label mismatch the database drops (issue #652). Pass 3
# (H) must reuse the registered qn (the C++ record-and-reuse rule, extended to
# (H) every language) and fan out only to variants whose registered type
# (H) matches the edge.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

MERGING_TS = """
class Calculator {
    add(value: number): this { return this; }
}
namespace Calculator {
    export function create(): Calculator {
        return new Calculator();
    }
}
function Logger(message: string): void {
    console.log(message);
}
namespace Logger {
    export function log(message: string): void { Logger(message); }
    export function info(message: string): void { Logger.log("INFO " + message); }
}
Logger("hi");
"""


def _node_keys(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }


def test_merged_namespace_members_emit_no_dangling_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "namespace_merging.ts").write_text(MERGING_TS)
    run_updater(temp_repo, mock_ingestor)

    node_keys = _node_keys(mock_ingestor)
    for rel_type in (cs.RelationshipType.CALLS, cs.RelationshipType.INSTANTIATES):
        for call in get_relationships(mock_ingestor, rel_type.value):
            from_label, _, from_qn = call.args[0]
            to_label, _, to_qn = call.args[2]
            assert (str(from_label), from_qn) in node_keys, call.args
            assert (str(to_label), to_qn) in node_keys, call.args


def test_merged_namespace_member_calls_attribute_to_registered_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "namespace_merging.ts").write_text(MERGING_TS)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    calls = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
    }
    # (H) info() calls the sibling namespace function log(); both live under
    # (H) the namespace's registered (duplicate-suffixed) qn.
    log_edges = [(f, t) for f, t in calls if f.endswith(".info") and t.endswith(".log")]
    assert log_edges, calls
    for from_qn, to_qn in log_edges:
        assert (
            from_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            == to_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        ), (from_qn, to_qn)
    # (H) The module-level Logger("hi") call still reaches the merged function.
    assert (f"{project}.namespace_merging", f"{project}.namespace_merging.Logger") in (
        calls
    ), calls
