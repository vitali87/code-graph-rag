"""Shared fixtures for extract/inline edit tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "extract_fixture"

REPORT_PY = """\
def build(items, factor):
    header = "report"
    total = 0
    count = 0
    for item in items:
        if item is None:
            continue
        scaled = item * factor
        total += scaled
        count += 1
    average = total / count if count else 0
    lines = [header]
    lines.append(f"total={total}")
    lines.append(f"average={average}")
    return "\\n".join(lines)
"""

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/report.py": REPORT_PY,
    "pkg/util.py": (
        "def wrapper(a, b=1):\n    return a * b + 1\n\n\ndef other():\n    return 2\n"
    ),
    "pkg/app.py": (
        "from pkg.util import wrapper, other\n\n\n"
        "def one():\n    return wrapper(2)\n\n\n"
        "def two(x):\n    return wrapper(x + 1, b=3)\n\n\n"
        "def three():\n    return wrapper(other(), 2) + other()\n"
    ),
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import one, three, two\n"
        "from pkg.report import build\n\n\n"
        "def test_calls():\n    assert (one(), two(1), three()) == (3, 7, 7)\n\n\n"
        "def test_build():\n"
        "    assert build([1, None, 3], 2) == 'report\\ntotal=8\\naverage=4.0'\n"
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


@pytest.fixture(name="extract_inline_repo")
def _extract_inline_repo(
    temp_repo: Path,
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    store, updater = _index(root)
    return root, store, updater


def _project_qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _smoke(root: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        check=True,
        capture_output=True,
    )


# --- extract ---------------------------------------------------------------------
