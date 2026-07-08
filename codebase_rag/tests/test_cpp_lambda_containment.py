# (H) A lambda in an out-of-line C++ method body (`void Directive::print()
# (H) { join(xs, [](){...}); }` in a .cpp, class declared in a header) must get
# (H) its DEFINES parent from the real class-anchored Method node. The parent
# (H) walk used to recompute a free-function qn that drops the `Class::` scope
# (H) (module.ns.print), which matches no node, so the database dropped the
# (H) edge and every such lambda was an orphan (issue #650: 234 on souffle).
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
class Directive {
public:
    void print() const;
};
}
"""

CPP = """
#include "Directive.h"
namespace souffle {

void Directive::print() const {
    invoke(1, [](int x) { return x + 1; });
}

int freeRun() {
    return invoke(2, [](int y) { return y * 2; });
}

}
"""


def _write_fixture(repo: Path) -> None:
    (repo / "Directive.h").write_text(HDR)
    (repo / "Directive.cpp").write_text(CPP)


def _lambda_defines(mock_ingestor: MagicMock) -> dict[str, tuple[str, str]]:
    parents: dict[str, tuple[str, str]] = {}
    for call in get_relationships(mock_ingestor, cs.RelationshipType.DEFINES.value):
        to_qn = str(call.args[2][2])
        if cs.PREFIX_LAMBDA in to_qn:
            parents[to_qn] = (str(call.args[0][0]), str(call.args[0][2]))
    return parents


TWO_NAMESPACES_HDR = """
#pragma once
namespace alpha {
class Widget {
public:
    void print() const;
};
}
namespace beta {
class Widget {
public:
    void print() const;
};
}
"""

TWO_NAMESPACES_CPP = """
#include "Widget.h"
namespace alpha {
void Widget::print() const {
    invoke(1, [](int x) { return x + 1; });
}
}
namespace beta {
void Widget::print() const {
    invoke(2, [](int y) { return y * 2; });
}
}
"""


def test_same_leaf_classes_bind_lambdas_to_their_own_namespace(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) alpha::Widget and beta::Widget share a leaf name; each out-of-line
    # (H) method's lambda must bind to its own namespace's Method node, not
    # (H) whichever Widget resolves first from the simple-name index.
    (temp_repo / "Widget.h").write_text(TWO_NAMESPACES_HDR)
    (temp_repo / "Widget.cpp").write_text(TWO_NAMESPACES_CPP)
    run_updater(temp_repo, mock_ingestor)

    parents = _lambda_defines(mock_ingestor)
    alpha_lambda = next(parents[qn] for qn in parents if "lambda_4_" in qn)
    beta_lambda = next(parents[qn] for qn in parents if "lambda_9_" in qn)
    assert ".alpha." in alpha_lambda[1], alpha_lambda
    assert ".beta." in beta_lambda[1], beta_lambda


def test_out_of_line_method_lambda_binds_to_method_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    parents = _lambda_defines(mock_ingestor)
    method_lambda = next(parents[qn] for qn in parents if "lambda_5_" in qn)
    assert method_lambda[0] == cs.NodeLabel.METHOD.value
    assert method_lambda[1].endswith("Directive.print")

    free_lambda = next(parents[qn] for qn in parents if "lambda_9_" in qn)
    assert free_lambda[1].endswith("freeRun")


def test_cpp_lambda_fixture_has_no_orphans_or_dangling_defines(
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
    # (H) Orphan detection counts only edges the database would keep, so this
    # (H) fails if the lambda's DEFINES parent is a phantom qn.
    assert ga.find_orphans(nodes, rels) == []
