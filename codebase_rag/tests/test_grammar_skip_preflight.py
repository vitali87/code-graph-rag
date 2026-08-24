"""The base-install grammar preflight must mirror updater eligibility (#1371).

The conftest fixture that skips grammar-dependent tests on a base install
decides from the files GraphUpdater would actually process. A file the run
ignores anyway (node_modules, hidden dirs, exclusions) must not gate a test
on its language's grammar. JavaScript is forced into the unavailable set here,
so the decision function is exercised deterministically on every install and a
regression fails the assertion instead of surfacing as an unexpected skip.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests import conftest as tests_conftest


@pytest.fixture
def _js_reported_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tests_conftest,
        "_unavailable_grammars",
        lambda: frozenset({cs.SupportedLanguage.JS}),
    )


def _updater_for(repo_path: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=MagicMock(),
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
    )


def test_ignored_js_file_does_not_gate_the_run(
    tmp_path: Path, _js_reported_unavailable: None
) -> None:
    (tmp_path / "included.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    dep = tmp_path / "node_modules" / "pkg"
    dep.mkdir(parents=True)
    (dep / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")

    missing = tests_conftest._grammars_missing_for(_updater_for(tmp_path))

    assert missing == frozenset()


def test_eligible_js_file_gates_the_run(
    tmp_path: Path, _js_reported_unavailable: None
) -> None:
    (tmp_path / "included.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("module.exports = 1;\n", encoding="utf-8")

    missing = tests_conftest._grammars_missing_for(_updater_for(tmp_path))

    assert missing == frozenset({cs.SupportedLanguage.JS})
