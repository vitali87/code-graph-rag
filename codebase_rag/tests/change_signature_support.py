"""Shared fixture project and helpers for the change_signature tests
(issue #1533): test_change_signature*.py import these.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "signature_fixture"
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a: int, b: str = 'x') -> str:\n    return b * a\n",
    "pkg/app.py": (
        "from pkg.util import helper\n\n\n"
        "def run():\n    return helper(2)\n\n\n"
        "def run_kw():\n    return helper(2, b='y')\n\n\n"
        "def run_both():\n    return helper(3, 'z')\n"
    ),
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 'xx'\n"
    ),
}
HELPER = f"{PROJECT}.pkg.util.helper"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _index(root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _project(temp_repo: Path, files: dict[str, str]) -> Path:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in files.items():
        _write(root, rel, text)
    return root


def indexed_fixture(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    """The FIXTURE project written under `temp_repo` and indexed."""
    root = _project(temp_repo, FIXTURE)
    store, updater = _index(root)
    return root, store, updater


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _smoke(root: Path, expression: str) -> None:
    """Run the rewritten fixture for real: a rewrite that parses can still
    be wrong, and only executing the call sites against the new definition
    shows they agree."""
    result = subprocess.run(
        [sys.executable, "-c", expression],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _set_resolution(store: _StatefulIngestor, caller: str, resolution: str) -> None:
    edge = next(
        e
        for e in store.edge_props
        if e[1] == caller and e[2] == cs.RelationshipType.CALLS.value and e[4] == HELPER
    )
    store.edge_props[edge][cs.KEY_RESOLUTION] = resolution
