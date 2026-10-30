"""A C++ lambda bound to a local is defined by the function it sits in.

`auto f = [] {...};` puts a `declaration` between the lambda and its
function, and the C++ spec counts a `declaration` as a module node too (a
file-scope variable's initializer belongs to the module). The parent walk
stopped there and handed the lambda to the Module; it must pass the
binding and keep climbing to the function, while a file-scope binding still
reaches the translation unit and stays the module's.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from evals.recorded_graph import RecordingIngestor

SOURCE = """\
int run() {
    auto twice = [](int x) { return 2 * x; };
    return twice(3);
}

int outer() {
    auto wrap = []() {
        auto inner = []() { return 1; };
        return inner();
    };
    return wrap();
}

auto top = []() { return 0; };
"""


def _lambda_parents(repo: Path, ingestor: RecordingIngestor) -> dict[str, str]:
    return {
        str(rel.target).rsplit(".", 1)[-1]: str(rel.source).removeprefix(
            f"{repo.name}."
        )
        for rel in get_relationships(ingestor, cs.RelationshipType.DEFINES.value)
        if ".lambda_" in str(rel.target)
    }


def test_a_lambda_bound_to_a_local_is_defined_by_its_function(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    (temp_repo / "shapes.cpp").write_text(SOURCE)
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    parents = _lambda_parents(temp_repo, mock_ingestor)

    assert parents["lambda_1_17"] == "shapes.run"
    assert parents["lambda_6_16"] == "shapes.outer"
    # A lambda bound inside another lambda is that lambda's.
    assert parents["lambda_7_21"] == "shapes.lambda_6_16"
    # Negative: the file-scope binding's initializer is still the module's.
    assert parents["lambda_13_11"] == "shapes"
