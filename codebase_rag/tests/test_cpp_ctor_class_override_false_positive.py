# (H) A nested class that inherits its enclosing class (souffle's BTree
# (H) `node::inner_node : node`) has a constructor whose name equals the nested
# (H) class's own name. The override pass matched any registry entry named
# (H) `parent_qn.method_name` without checking it is a METHOD, so the ctor
# (H) emitted OVERRIDES with a Method label onto the nested CLASS node -- a
# (H) label-mismatched phantom the database drops (issue #652).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

TREE_HDR = """
#pragma once
struct node {
    int size() const { return 1; }

    struct inner_node : node {
        inner_node(int n) {}
        int size() const { return n_; }
        int n_;
    };
};
"""


def test_ctor_does_not_override_same_named_nested_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "tree.h").write_text(TREE_HDR)
    run_updater(temp_repo, mock_ingestor)

    method_qns = {
        c.args[1].get("qualified_name")
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.METHOD.value
    }
    overrides = get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES.value)
    size_overrides = []
    for call in overrides:
        assert call.args[2][2] in method_qns, call.args
        if str(call.args[0][2]).endswith(".inner_node.size"):
            size_overrides.append(call)
    # (H) The real override (size on the base) must survive the type check.
    assert size_overrides
