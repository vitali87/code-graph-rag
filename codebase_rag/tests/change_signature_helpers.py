# change_signature (issue #1533): the definition and every graph-known call
# site are rewritten per an explicit parameter mapping; sites the mapping
# cannot complete, or that the graph resolved by guesswork, are left
# untouched and listed as unmapped. The graph is the in-memory stateful
# ingestor; a real index of a fixture repo drives every case.

from __future__ import annotations

from pathlib import Path

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


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


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


def repo_fixture(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    store, updater = _index(root)
    return root, store, updater


def _qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _smoke(root: Path) -> None:
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        check=True,
        capture_output=True,
    )
