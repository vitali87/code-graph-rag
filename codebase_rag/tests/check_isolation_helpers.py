"""`cgr check --isolated` measures the working tree without keeping the re-ingest.

Issue #1718. The check re-ingests the files that differ from the base, which
brings the shared graph up to the working tree: a second run on the same edit
reports nothing, and a prior check makes a later `--fail-on-found` pass for
the same edits. Isolated mode captures the subgraph the re-ingest is about to
replace, runs the same check, and puts the capture back, so the graph and the
on-disk hash cache read exactly as they did before, and the check can be
rerun.

The store is the eval emulator, which models the module-subtree delete and
the capture queries by value; the same edit is replayed against a real
Memgraph in the integration tier.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codebase_rag.capture import CaptureSelection
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import StructuralDelta
from evals.cgr_graph import _StatefulIngestor

PROJECT = "iso_fixture"

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a):\n    return a + 1\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
    "main.py": "from pkg.app import run\n\n\ndef main():\n    run()\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 2\n"
    ),
}

_TIMINGS = ("reingest_ms", "delta_ms")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    return root, store


def _updater(
    store: _StatefulIngestor, root: Path, capture: CaptureSelection | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        capture=capture,
    )


def _state(store: _StatefulIngestor) -> tuple[dict, set, dict]:
    return (
        copy.deepcopy(store.nodes),
        set(store.edges),
        copy.deepcopy(store.edge_props),
    )


def _check(root: Path, store: _StatefulIngestor, *, isolated: bool) -> StructuralDelta:
    parsers, queries = load_parsers()
    return run_check(root, "HEAD", PROJECT, store, parsers, queries, isolated=isolated)


def _findings(delta: StructuralDelta) -> dict[str, Any]:
    return {key: value for key, value in delta.items() if key not in _TIMINGS}


def _edit(root: Path) -> None:
    """One edit of every kind the re-ingest handles differently.

    A renamed definition (its callers dangle), a deleted module, a new
    module in a new directory (a Folder the graph has never seen), a new
    external import, and a deleted package indicator (`tests/` flips from
    Package to Folder).
    """
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("helper", "assist"))
    (root / "main.py").unlink()
    _write(root, "lib/tool.py", "import os\n\n\ndef tool():\n    return os.sep\n")
    (root / "tests" / "__init__.py").unlink()


def _labelled(store: _StatefulIngestor, label: str) -> set[Any]:
    return {uid for (node_label, uid) in store.nodes if node_label == label}
