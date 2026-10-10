"""Every sync repairs only its own project's unattached notes (issue #3236).

The full run, the "already in sync" fast path and each incremental
re-ingest re-anchor glosses; the repair they ran covered every unattached
note in the shared graph, at two whole-graph lookups per project holding
one, so a few LOST notes anywhere slowed every other project's syncs and
agent edits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import graph_updater
from codebase_rag.gloss_repair import RepairReport
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


def test_every_sync_path_scopes_the_repair_to_its_project(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scopes: list[str | None] = []

    def recording(*_args: object, project_name: str | None = None) -> RepairReport:
        scopes.append(project_name)
        return RepairReport(moved=[], ambiguous=[], lost=[])

    monkeypatch.setattr(graph_updater, "repair_unanchored", recording)
    (temp_repo / "a.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        project_name="app",
    )

    updater.run(force=True)
    updater.run()  # nothing changed: the "already in sync" path
    (temp_repo / "a.py").write_text("def f(x):\n    return x + 2\n", encoding="utf-8")
    updater.reingest(["a.py"])

    assert scopes and set(scopes) == {"app"}, scopes
    assert len(scopes) >= 3, scopes
