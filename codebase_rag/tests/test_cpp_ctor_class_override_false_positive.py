# A nested class that inherits its enclosing class (souffle's BTree
# `node::inner_node : node`) has a constructor whose name equals the nested
# class's own name. The override pass matched any registry entry named
# `parent_qn.method_name` without checking it is a METHOD, so the ctor
# emitted OVERRIDES with a Method label onto the nested CLASS node -- a
# label-mismatched phantom the database drops (issue #652).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_nodes, get_relationships, run_updater

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
        c.properties.get("qualified_name")
        for c in get_nodes(mock_ingestor)
        if str(c.label) == cs.NodeLabel.METHOD.value
    }
    overrides = get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES.value)
    size_overrides = []
    for call in overrides:
        assert call.target in method_qns, call
        if str(call.source).endswith(".inner_node.size"):
            size_overrides.append(call)
    # The real override (size on the base) must survive the type check.
    assert size_overrides
