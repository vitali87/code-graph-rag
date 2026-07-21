# An out-of-line C++ method whose class cannot be resolved (macro-corrupted
# declarations, or no such class parsed at all) used to get a
# DEFINES_METHOD edge to a phantom fallback class qn, which the database
# drops, orphaning the Method node (issue #650, the span.h make_span case).
# The node must instead anchor to its module so it stays reachable.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag import graph_audit as ga
from codebase_rag.tests.conftest import get_relationships, run_updater
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord

CPP = """
int Mystery::bar() {
    return 1;
}
"""


def test_unresolvable_out_of_line_method_anchors_to_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "mystery.cpp").write_text(CPP)
    run_updater(temp_repo, mock_ingestor)

    method_qns = {
        c.args[1]["qualified_name"]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.METHOD.value
    }
    bar_qn = next(qn for qn in method_qns if qn.endswith(".bar"))

    module_defines = {
        str(call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.DEFINES.value)
        if str(call.args[0][0]) == cs.NodeLabel.MODULE.value
    }
    assert bar_qn in module_defines

    nodes = [
        GraphNodeRecord(str(c.args[0]), c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
    ]
    rels = [
        GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
    ]
    assert ga.find_orphans(nodes, rels) == []
