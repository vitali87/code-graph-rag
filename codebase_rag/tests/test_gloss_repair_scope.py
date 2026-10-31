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

from codebase_rag import constants as cs
from codebase_rag import graph_updater
from codebase_rag.gloss_repair import RepairReport
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
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

    assert scopes, scopes
    assert set(scopes) == {"app"}, scopes
    assert len(scopes) >= 3, scopes


# Three projects in one graph: `app`, `app.sub` nested under its name, and
# `other`. Every note names a definition that exists and records a hash its
# subject no longer carries, so any pass that reached it would attach it,
# restore its mention or grade it STALE.
_SHARED_GRAPH = """
CREATE (:Project {name: 'app'}), (:Project {name: 'app.sub'}),
       (:Project {name: 'other'}),
       (:Function {qualified_name: 'app.m.f', anchor_hash: 'ah1:new'}),
       (:Function {qualified_name: 'app.m.k'}),
       (:Function {qualified_name: 'app.sub.m.g', anchor_hash: 'ah1:new'}),
       (:Function {qualified_name: 'app.sub.m.k'}),
       (oh:Function {qualified_name: 'other.m.h', anchor_hash: 'ah1:new'}),
       (:Function {qualified_name: 'other.m.k'}),
       (:Gloss {qualified_name: 'gloss:app', project: 'app',
                target_qn: 'app.m.f', target_hash: 'ah1:old',
                mention_qns: ['app.m.k'], anchor_state: 'LOST'}),
       (:Gloss {qualified_name: 'gloss:app-legacy',
                target_qn: 'app.m.f', target_hash: 'ah1:old',
                mention_qns: ['app.m.k'], anchor_state: 'LOST'}),
       (:Gloss {qualified_name: 'gloss:sub-legacy',
                target_qn: 'app.sub.m.g', target_hash: 'ah1:old',
                mention_qns: ['app.sub.m.k'], anchor_state: 'LOST'}),
       (:Gloss {qualified_name: 'gloss:other', project: 'other',
                target_qn: 'other.m.h', target_hash: 'ah1:old',
                mention_qns: ['other.m.k'], anchor_state: 'LOST'}),
       (:Gloss {qualified_name: 'gloss:other-attached', project: 'other',
                target_qn: 'other.m.h', target_hash: 'ah1:old',
                mention_qns: ['other.m.k'],
                anchor_state: 'EXACT'})-[:ANNOTATES]->(oh)
"""
_NOTES = """MATCH (g:Gloss)
OPTIONAL MATCH (g)-[:ANNOTATES]->(t)
OPTIONAL MATCH (g)-[:MENTIONS]->(m)
RETURN g.qualified_name AS qn, g.anchor_state AS state,
       count(DISTINCT t) AS subjects, count(DISTINCT m) AS mentions"""


def test_the_name_tier_mentions_and_grade_touch_only_the_syncing_project(
    tmp_path: Path,
) -> None:
    # `app`'s sync attaches, restores and grades `app`'s notes, a legacy one
    # (no `project`) by its target's prefix. A legacy note under `app.sub.`
    # is `app.sub`'s, and `other`'s notes, attached or not, are left exactly
    # as their own syncs left them (CodeRabbit, PR #3319).
    with MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(tmp_path / "graph.sqlite"),
    ) as store:
        store.execute_write(_SHARED_GRAPH)
        updater = GraphUpdater(
            ingestor=store,
            repo_path=tmp_path,
            parsers={},
            queries={},
            project_name="app",
        )

        updater._reanchor_glosses()

        notes = {
            row["qn"]: (row["state"], row["subjects"], row["mentions"])
            for row in store.fetch_all(_NOTES)
        }
    stale = cs.GlossAnchorState.STALE.value
    assert notes["gloss:app"] == (stale, 1, 1)
    assert notes["gloss:app-legacy"] == (stale, 1, 1)
    assert notes["gloss:sub-legacy"] == ("LOST", 0, 0)
    assert notes["gloss:other"] == ("LOST", 0, 0)
    assert notes["gloss:other-attached"] == ("EXACT", 1, 0)
