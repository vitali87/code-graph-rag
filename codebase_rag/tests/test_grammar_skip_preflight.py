"""The base-install grammar preflight must mirror updater eligibility (#1371).

The conftest fixture that skips grammar-dependent tests on a base install
decides from the files GraphUpdater would actually process. A file the run
ignores anyway (node_modules, hidden dirs, exclusions) must not gate a test
on its language's grammar. On a full install the preflight is a no-op and
this passes trivially; the assertion bites on a base install.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater


def test_ignored_js_file_does_not_gate_python_test(tmp_path: Path) -> None:
    (tmp_path / "included.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    dep = tmp_path / "node_modules" / "pkg"
    dep.mkdir(parents=True)
    (dep / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")

    updater = create_and_run_updater(tmp_path, MagicMock())

    assert updater is not None
