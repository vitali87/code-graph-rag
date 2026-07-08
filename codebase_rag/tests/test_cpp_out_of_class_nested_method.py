# (H) A nested class defined out-of-class (`class Outer::Inner : ... {}` in a
# (H) header) registers its name as the literal `Outer::Inner`, so an
# (H) out-of-line method in a .cpp (`bool Inner::transform()`, typically via a
# (H) `using Inner = Outer::Inner;` alias) failed the leaf lookup and the
# (H) endswith guard, fell back to a phantom class qn, and both the method and
# (H) its DEFINES_METHOD edge dangled (issue #650: the ~21-method tail of
# (H) #496/#512 on souffle's MagicSet.cpp).
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

class MagicSetTransformer {
public:
    class NormaliseDatabaseTransformer;
};

class MagicSetTransformer::NormaliseDatabaseTransformer {
public:
    bool transform();
};

}
"""

CPP = """
#include "MagicSet.h"
namespace souffle {

using NormaliseDatabaseTransformer = MagicSetTransformer::NormaliseDatabaseTransformer;

bool NormaliseDatabaseTransformer::transform() {
    return true;
}

}
"""


def _write_fixture(repo: Path) -> None:
    (repo / "MagicSet.h").write_text(HDR)
    (repo / "MagicSet.cpp").write_text(CPP)


def test_out_of_line_method_of_nested_class_binds_to_real_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    class_qns = {
        c.args[1]["qualified_name"]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.CLASS.value
    }
    # (H) The forward-declared member also mints a dot-form Class node
    # (H) (Outer.Inner); the defined class keeps the literal Outer::Inner
    # (H) segment, and that is the node out-of-line methods must bind to.
    nested_class_qn = next(
        qn for qn in class_qns if qn.endswith("::NormaliseDatabaseTransformer")
    )

    method_parents = {
        str(call.args[2][2]): str(call.args[0][2])
        for call in get_relationships(
            mock_ingestor, cs.RelationshipType.DEFINES_METHOD.value
        )
    }
    transform_edges = {
        qn: parent for qn, parent in method_parents.items() if qn.endswith(".transform")
    }
    assert transform_edges, method_parents
    for qn, parent in transform_edges.items():
        assert parent == nested_class_qn, (qn, parent)
        assert qn.startswith(nested_class_qn), qn


def test_fixture_has_no_orphans(temp_repo: Path, mock_ingestor: MagicMock) -> None:
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
