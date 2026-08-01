# Types nested inside a C++ class body (class/enum/union members) must get
# a DEFINES edge from the enclosing Class node. The parent walk used to
# fall back to a qn-trim labeled Method (a phantom: right qn, wrong label),
# which the database drops, orphaning every class-nested type (issue #650:
# 17 Class + 16 Enum + 1 Union orphans on souffle).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag import graph_audit as ga
from codebase_rag.tests.conftest import get_relationships, run_updater
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord

HDR = """
#pragma once
namespace souffle {
class Program {
public:
    class RelationInfo {
    public:
        int arity;
    };
    enum class State { Ok, Bad };
    union Cell { int i; float f; };
    void run();
};
}
"""


def _write_fixture(repo: Path) -> None:
    (repo / "Program.h").write_text(HDR)


def test_class_nested_types_bind_to_enclosing_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    parents: dict[str, tuple[str, str]] = {}
    for call in get_relationships(mock_ingestor, cs.RelationshipType.DEFINES.value):
        to_qn = str(call.args[2][2])
        parents[to_qn.rsplit(".", 1)[-1]] = (
            str(call.args[0][0]),
            str(call.args[0][2]),
        )

    for nested in ("RelationInfo", "State", "Cell"):
        label, qn = parents[nested]
        assert label == cs.NodeLabel.CLASS.value, (nested, label)
        assert qn.endswith(".Program"), (nested, qn)


def test_cpp_nested_type_fixture_has_no_orphans(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    nodes = [
        GraphNodeRecord(str(c.args[0]), c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
    ]
    rels = [
        GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
    ]
    assert ga.find_orphans(nodes, rels) == []
