from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from codebase_rag.parsers.type_inference import TypeInferenceEngine


class NodeStub:
    """Minimal Tree-sitter node stub for testing traversal logic."""

    def __init__(
        self,
        node_type: str,
        *,
        children: list[NodeStub] | None = None,
        text: bytes | None = None,
        fields: dict[str, NodeStub] | None = None,
    ) -> None:
        self.type = node_type
        self._children = children or []
        self.text = text
        self._fields = fields or {}

    @property
    def children(self) -> list[NodeStub]:
        return list(self._children)

    def child_by_field_name(self, name: str) -> NodeStub | None:
        return self._fields.get(name)


def _build_deep_assignment_chain(depth: int) -> NodeStub:
    """Create a deeply nested assignment chain exceeding recursion limits."""

    next_node: NodeStub | None = None
    for index in range(depth):
        attr = NodeStub(
            "attribute",
            text=f"self.attr{index}".encode(),
        )
        right = NodeStub("identifier", text=b"value")
        current = NodeStub(
            "assignment",
            children=[attr, right] + ([next_node] if next_node else []),
            fields={"left": attr, "right": right},
        )
        next_node = current

    return NodeStub("block", children=[next_node] if next_node else [])


def _build_deep_return_tree(depth: int) -> NodeStub:
    """Create a deeply nested tree containing many return statements."""

    current: NodeStub = NodeStub("return_statement")
    for _ in range(depth):
        return_node = NodeStub("return_statement")
        current = NodeStub("block", children=[return_node, current])

    return current


def _make_engine() -> TypeInferenceEngine:
    return TypeInferenceEngine(
        import_processor=MagicMock(),
        function_registry=MagicMock(),
        simple_name_lookup=defaultdict(set),
        repo_path=Path("."),
        project_name="proj",
        ast_cache=MagicMock(),
        queries={},
        module_qn_to_file_path={},
        class_inheritance={},
    )


def test_analyze_self_assignments_handles_deep_tree_without_recursion_error() -> None:
    engine = _make_engine()

    engine._infer_type_from_expression = MagicMock(return_value="MockType")  # type: ignore[method-assign]

    root = _build_deep_assignment_chain(depth=1500)
    local_types: dict[str, Any] = {}

    engine._analyze_self_assignments(root, local_types, "proj.module")  # ty: ignore[invalid-argument-type]  # (H) NodeStub not Node

    assert local_types, "Expected at least one inferred instance variable"
    assert engine._infer_type_from_expression.call_count == 1500  # type: ignore[attr-defined]


def test_find_return_statements_handles_deep_tree_without_recursion_error() -> None:
    engine = _make_engine()

    root = _build_deep_return_tree(depth=1500)
    returns: list[NodeStub] = []

    engine._find_return_statements(root, returns)  # ty: ignore[invalid-argument-type]  # (H) NodeStub not Node

    assert len(returns) == 1501
