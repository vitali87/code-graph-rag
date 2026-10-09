"""`check` reports a dangling caller where the call is in the working tree.

The site came from the base graph, so it kept the base line: deleting a
function above its caller reported the call past the end of the file
(`app.py:10` in a 6-line file) or at another function's line, while the
same report's `signature_changes` used working-tree lines (issue #3171).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta, observe
from evals.cgr_graph import _StatefulIngestor

PROJECT = "chk"
_APP = (
    "def helper():\n    return 1\n\n\n"
    "def other():\n    return 2\n\n\n"
    "def uses_helper():\n    return helper() + other()\n"
)
_ELSEWHERE = "from app import helper\n\n\ndef far():\n    return helper()\n"


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def indexed(tmp_path: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = tmp_path / PROJECT
    _write(root, "app.py", _APP)
    _write(root, "far.py", _ELSEWHERE)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return root, store, updater


def _check(
    root: Path, store: _StatefulIngestor, updater: GraphUpdater, changed: list[str]
) -> StructuralDelta:
    return observe(
        store.fetch_all,
        PROJECT,
        changed,
        lambda: updater.reingest(changed, deleted=[]),
        repo_root=root,
    )


def _sites(delta: StructuralDelta) -> dict[str, tuple[int | None, int | None, str]]:
    return {
        d["caller"].removeprefix(f"{PROJECT}."): (
            d["line"],
            d["col"],
            d.get("line_from", ""),
        )
        for d in delta["dangling_callers"]
    }


def test_a_caller_below_a_deleted_function_is_reported_where_it_is_now(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app.py", _APP.replace("def helper():\n    return 1\n\n\n", ""))
    sites = _sites(_check(root, store, updater, ["app.py"]))
    edited = (root / "app.py").read_text().splitlines()
    assert sites["app.uses_helper"] == (6, 11, ""), sites
    assert edited[5][11:].startswith("helper()"), edited


def test_a_caller_in_an_untouched_file_keeps_its_line(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # Negative: far.py was not edited, so its base line is its line.
    root, store, updater = indexed
    _write(root, "app.py", _APP.replace("def helper():\n    return 1\n\n\n", ""))
    assert _sites(_check(root, store, updater, ["app.py"]))["far.far"] == (5, 11, "")


def test_a_caller_whose_body_changed_says_its_line_is_the_base_one(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # Negative: an edit inside the caller may have moved the call within
    # it, so the line is not shifted and is marked as the base ref's.
    root, store, updater = indexed
    edited = _APP.replace("def helper():\n    return 1\n\n\n", "").replace(
        "    return helper() + other()",
        "    x = other()\n    y = other()\n    return helper() + x + y",
    )
    _write(root, "app.py", edited)
    sites = _sites(_check(root, store, updater, ["app.py"]))
    assert sites["app.uses_helper"] == (10, 11, cs.DANGLING_LINE_FROM_BASE), sites
