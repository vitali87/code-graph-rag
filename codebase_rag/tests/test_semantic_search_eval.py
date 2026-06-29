from pathlib import Path

import pytest

from codebase_rag.utils.dependencies import has_semantic_dependencies
from evals import constants as ec
from evals.semantic_search import (
    SemanticCase,
    cgr_semantic_ranking,
    function_snippets,
    score_semantic,
)

needs_semantic = pytest.mark.skipif(
    not has_semantic_dependencies(), reason="semantic extra not installed"
)


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "ops.py").write_text(
        "import json\n\n\n"
        "def load_json_file(path):\n"
        "    with open(path) as handle:\n"
        "        return json.load(handle)\n\n\n"
        "def send_email(recipient, body):\n"
        "    server = connect_smtp()\n"
        "    server.sendmail(recipient, body)\n\n\n"
        "def compute_sales_tax(amount, rate):\n"
        "    return amount * rate\n\n\n"
        "def connect_smtp():\n"
        "    return object()\n",
        encoding="utf-8",
    )


_CASES = [
    SemanticCase("read and parse a json file from disk", "proj.ops.load_json_file"),
    SemanticCase("send an email message to a recipient", "proj.ops.send_email"),
    SemanticCase("calculate tax on a purchase amount", "proj.ops.compute_sales_tax"),
]


@needs_semantic
def test_function_snippets_extracted_from_graph(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    snippets = function_snippets(src, "proj")
    assert "proj.ops.load_json_file" in snippets
    assert "json.load" in snippets["proj.ops.load_json_file"]


@needs_semantic
def test_cgr_semantic_search_retrieves_expected_function(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src)
    queries = [case.query for case in _CASES]
    ranking = cgr_semantic_ranking(src, "proj", queries, ec.SEMANTIC_TOP_K)
    result = score_semantic(_CASES, ranking)
    row = next(r for r in result.rows if r["label"] == ec.SEMANTIC_LABEL)
    # (H) Each query's clearly-relevant function should rank in the top k.
    assert row["recall"] == 1.0
    assert row["fn"] == 0


def test_score_semantic_counts_misses() -> None:
    cases = [
        SemanticCase("q1", "proj.a"),
        SemanticCase("q2", "proj.b"),
    ]
    ranking = {"q1": ["proj.a", "proj.x"], "q2": ["proj.y"]}
    result = score_semantic(cases, ranking)
    row = next(r for r in result.rows if r["label"] == ec.SEMANTIC_LABEL)
    assert (row["tp"], row["fn"]) == (1, 1)
    assert row["recall"] == 0.5
