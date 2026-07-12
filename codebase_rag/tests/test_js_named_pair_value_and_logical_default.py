# (H) Two express residuals:
# (H) 1. a NAMED function expression as an object-pair value (`get: function
# (H)    getrouter() {...}`) registers by its OWN name, which neither the pair-key
# (H)    nor the position candidate matches;
# (H) 2. a function expression behind a LOGICAL DEFAULT (`done = done ||
# (H)    function (err, str) {...}`) sits in a binary RHS the assignment walker
# (H)    never descended into.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _refs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }


def test_named_pair_value_function_expression_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "exnpv"
    root.mkdir(parents=True)
    (root / "app.js").write_text(
        "exports.init = function () {\n"
        "  Object.defineProperty(this, 'router', {\n"
        "    get: function getrouter() {\n"
        "      return 1\n"
        "    }\n"
        "  })\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(t.endswith(".getrouter") for _, t in refs), sorted(
        t for _, t in refs if "getrouter" in t or "anonymous" in t
    )


def test_logical_default_function_expression_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "exlod"
    root.mkdir(parents=True)
    (root / "res.js").write_text(
        "exports.render = function (view, done) {\n"
        "  done = done || function (err, str) {\n"
        "    return str\n"
        "  }\n"
        "  return done(null, view)\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(".anonymous_1_" in t for _, t in refs), sorted(
        t for _, t in refs if "anonymous" in t
    )
