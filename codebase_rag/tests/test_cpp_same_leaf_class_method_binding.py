# Two classes sharing a leaf name (souffle::ast::Type and
# souffle::ast::analysis::Type) each define out-of-line methods in their
# own .cpp. The class lookup used to iterate an unordered candidate set
# and accept ANY same-leaf class, so a method could bind to the wrong
# class nondeterministically; Pass 3 then re-resolved independently and
# any disagreement became a phantom caller whose CALLS the database drops
# (issue #652). The definition pass now resolves once, namespace-scoped
# and deterministic, records the decision, and call attribution reuses it.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

AST_HDR = """
#pragma once
namespace souffle::ast {
class Type {
public:
    Type(int kind);
    int getKind() const;
};
int check(int x);
}
"""

AST_CPP = """
#include "ast/Type.h"
namespace souffle::ast {
int check(int x) { return x; }
Type::Type(int kind) : kind_(check(kind)) {}
int Type::getKind() const { return check(0); }
}
"""

ANALYSIS_HDR = """
#pragma once
namespace souffle::ast::analysis {
class Type {
public:
    Type(int id);
    int getId() const;
};
}
"""

ANALYSIS_CPP = """
#include "ast/analysis/Type.h"
namespace souffle::ast::analysis {
Type::Type(int id) : id_(id) {}
int Type::getId() const { return 1; }
}
"""


def _write_fixture(repo: Path) -> None:
    (repo / "src" / "ast" / "analysis").mkdir(parents=True)
    (repo / "src" / "ast" / "Type.h").write_text(AST_HDR)
    (repo / "src" / "ast" / "Type.cpp").write_text(AST_CPP)
    (repo / "src" / "ast" / "analysis" / "Type.h").write_text(ANALYSIS_HDR)
    (repo / "src" / "ast" / "analysis" / "Type.cpp").write_text(ANALYSIS_CPP)


def test_same_leaf_methods_bind_to_their_own_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    method_qns = {
        c.args[1]["qualified_name"]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.METHOD.value
    }
    assert any(".ast.Type.h.souffle.ast.Type.getKind" in qn for qn in method_qns), (
        method_qns
    )
    assert any(
        "analysis.Type.h.souffle.ast.analysis.Type.getId" in qn for qn in method_qns
    ), method_qns


def test_same_leaf_out_of_line_callers_are_real_nodes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)
    run_updater(temp_repo, mock_ingestor)

    node_keys = {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }
    check_calls = [
        call
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if str(call.args[2][2]).endswith(".check")
    ]
    assert check_calls
    for call in check_calls:
        from_label, _, from_qn = call.args[0]
        if str(from_label) == cs.NodeLabel.MODULE.value:
            continue
        assert (str(from_label), from_qn) in node_keys, call.args
        assert ".ast.Type.h.souffle.ast.Type." in str(from_qn), call.args
