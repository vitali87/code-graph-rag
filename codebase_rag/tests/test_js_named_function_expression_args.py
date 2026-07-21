# A NAMED function expression passed as a call argument (express's
# `this.on('mount', function onmount(parent) {...})`, `defineGetter(req,
# 'path', function path() {...})`) registers by its own NAME, but the inline
# argument reference is built from the arg's POSITION only, so the named
# node never matches and reports dead. Name candidates must be tried too.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _refs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "REFERENCES")
    }


def test_named_function_expression_arg_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "exarg"
    root.mkdir(parents=True)
    (root / "app.js").write_text(
        "exports.init = function () {\n"
        "  this.on('mount', function onmount(parent) {\n"
        "    return parent\n"
        "  })\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(t.endswith(".onmount") for _, t in refs), sorted(
        t for _, t in refs if "onmount" in t or "anonymous" in t
    )


def test_named_function_expression_getter_value_is_referenced(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # express's request.js shape: the named fn-expr is the third argument of
    # a module-scope helper call.
    root = temp_repo / "exget"
    root.mkdir(parents=True)
    (root / "request.js").write_text(
        "function defineGetter(obj, name, getter) {\n"
        "  Object.defineProperty(obj, name, { get: getter })\n"
        "}\n"
        "const req = {}\n"
        "defineGetter(req, 'secure', function secure() {\n"
        "  return true\n"
        "})\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    refs = _refs(mock_ingestor)
    assert any(t.endswith(".secure") for _, t in refs), sorted(
        t for _, t in refs if "secure" in t or "anonymous" in t
    )
