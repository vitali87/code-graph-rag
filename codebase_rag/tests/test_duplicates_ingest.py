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


CPP_TEMPLATE_A = """
template <typename T>
const T& pick_first(const std::vector<T>& items, int fallback) {
    for (const auto& item : items) {
        if (item.weight > fallback) {
            return item;
        }
    }
    return items.front();
}
"""

CPP_TEMPLATE_B = """
template <typename V>
const V& choose_lead(const std::vector<V>& boxes, int floor) {
    for (const auto& box : boxes) {
        if (box.weight > floor) {
            return box;
        }
    }
    return boxes.front();
}
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


def test_cpp_template_function_gets_a_fingerprint(
    temp_repo: Path, mock_ingestor: _MockIngestor
) -> None:
    # A C++ template function is registered on its template_declaration
    # wrapper, which has no `body` field of its own - the body lives on the
    # inner function_definition. Skipping it silently exempts every template
    # function from clone detection (fmt: 1012 of its symbols).
    (temp_repo / "first.cpp").write_text(CPP_TEMPLATE_A)
    (temp_repo / "second.cpp").write_text(CPP_TEMPLATE_B)
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    functions = _props_by_name(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    original = functions["pick_first"]
    renamed = functions["choose_lead"]
    assert original.get(cs.KEY_AST_FINGERPRINT) is not None
    assert original[cs.KEY_AST_FINGERPRINT] == renamed[cs.KEY_AST_FINGERPRINT]
    assert original[cs.KEY_AST_BRANCH_FINGERPRINTS]


CSHARP_ARROW_A = """
class PriceSheet {
    decimal Total => items.Sum(i => i.Price * factor) + shipping.Base + Fees(items);
}
"""

CSHARP_ARROW_B = """
class WeightSheet {
    decimal Load => boxes.Sum(b => b.Mass * scale) + pallet.Tare + Slack(boxes);
}
"""


def test_csharp_expression_bodied_property_gets_a_fingerprint(
    temp_repo: Path, mock_ingestor: _MockIngestor
) -> None:
    # A C# property_declaration NEVER has a `body` field: an expression-bodied
    # property carries its logic in a plain arrow_expression_clause child, so
    # every such property silently skipped clone detection (Humanizer: 2311
    # skipped symbols, largely properties).
    (temp_repo / "First.cs").write_text(CSHARP_ARROW_A)
    (temp_repo / "Second.cs").write_text(CSHARP_ARROW_B)
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="c_sharp")

    methods = _props_by_name(mock_ingestor, cs.NodeLabel.METHOD.value)
    original = methods["Total"]
    renamed = methods["Load"]
    assert original.get(cs.KEY_AST_FINGERPRINT) is not None
    assert original[cs.KEY_AST_FINGERPRINT] == renamed[cs.KEY_AST_FINGERPRINT]
