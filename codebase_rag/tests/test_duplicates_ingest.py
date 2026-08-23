"""End-to-end: structural fingerprints land on Function/Method nodes at ingest.

The conftest graph audit that runs inside create_and_run_updater also proves
the new properties are schema-registered (an unregistered property raises
UNDOCUMENTED_PROPERTY).
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    _MockIngestor,
    create_and_run_updater,
    get_nodes,
)

TOTAL_PRICE = """
def total_price(items):
    result = 0
    for item in items:
        result += item.price
    return result
"""

SUM_WEIGHTS = """
def sum_weights(boxes):
    acc = 0
    for box in boxes:
        acc += box.weight
    return acc
"""

COUNTER_CLASS = """
class Counter:
    def total_price(self, items):
        result = 0
        for item in items:
            result += item.price
        return result
"""


def _props_by_name(
    mock_ingestor: _MockIngestor, label: str
) -> dict[str, dict[str, object]]:
    return {call[0][1]["name"]: call[0][1] for call in get_nodes(mock_ingestor, label)}


def test_function_fingerprints_match_across_renamed_copies(
    temp_repo: Path, mock_ingestor: _MockIngestor
) -> None:
    (temp_repo / "billing.py").write_text(TOTAL_PRICE)
    (temp_repo / "shipping.py").write_text(SUM_WEIGHTS)
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="python")

    functions = _props_by_name(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    original = functions["total_price"]
    renamed = functions["sum_weights"]
    assert original[cs.KEY_AST_FINGERPRINT] == renamed[cs.KEY_AST_FINGERPRINT]
    assert (
        original[cs.KEY_AST_FINGERPRINT_NODES] == renamed[cs.KEY_AST_FINGERPRINT_NODES]
    )
    assert (
        original[cs.KEY_AST_BRANCH_FINGERPRINTS]
        == renamed[cs.KEY_AST_BRANCH_FINGERPRINTS]
    )
    assert original[cs.KEY_AST_BRANCH_FINGERPRINTS]


def test_different_function_gets_a_different_fingerprint(
    temp_repo: Path, mock_ingestor: _MockIngestor
) -> None:
    (temp_repo / "billing.py").write_text(TOTAL_PRICE)
    (temp_repo / "other.py").write_text("def greet(name):\n    return name.upper()\n")
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="python")

    functions = _props_by_name(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    assert (
        functions["total_price"][cs.KEY_AST_FINGERPRINT]
        != functions["greet"][cs.KEY_AST_FINGERPRINT]
    )


def test_method_fingerprint_matches_the_module_function_twin(
    temp_repo: Path, mock_ingestor: _MockIngestor
) -> None:
    (temp_repo / "billing.py").write_text(TOTAL_PRICE)
    (temp_repo / "counter.py").write_text(COUNTER_CLASS)
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="python")

    functions = _props_by_name(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    methods = _props_by_name(mock_ingestor, cs.NodeLabel.METHOD.value)
    # Only the body is fingerprinted (self lives in the params), so a method
    # whose body copies a module function is the same clone.
    method_props = methods["total_price"]
    assert isinstance(method_props[cs.KEY_AST_FINGERPRINT_NODES], int)
    assert isinstance(method_props[cs.KEY_AST_BRANCH_FINGERPRINTS], list)
    assert (
        method_props[cs.KEY_AST_FINGERPRINT]
        == functions["total_price"][cs.KEY_AST_FINGERPRINT]
    )
