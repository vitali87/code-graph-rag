"""Issue #2918: an incremental sync reads only its own project's inbound edges.

The inbound-edge capture matched `target.path IN $paths` over every edge in
the shared database, so it scanned the whole graph once per sync and
returned other projects' edges into their own same-path files; and no
index served the per-file `MATCH (m:Module {path: ...})`, so each module
delete scanned every node too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, DIALECT_NEO4J, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_ingestor_backend_selection import _ingestor
from codebase_rag.types_defs import ResultRow
from evals.cgr_graph import _StatefulIngestor

FILES = {
    "util.py": "def helper():\n    return 1\n",
    "app.py": "from util import helper\n\n\ndef run():\n    return helper()\n",
}


def _updater(store: _StatefulIngestor, root: Path, project: str) -> GraphUpdater:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


@pytest.fixture
def twins(temp_repo: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    # Two projects in one graph with the same layout, as two worktrees of a
    # repository or a re-index under a new name are.
    store = _StatefulIngestor()
    for project in ("beta", "alpha"):
        _updater(store, temp_repo / project, project).run(force=True)
    return store, _updater(store, temp_repo / "alpha", "alpha")


def _row(target_label: str, target_qn: str) -> ResultRow:
    return {
        cs.KEY_CALLER_LABEL: cs.NodeLabel.MODULE.value,
        cs.KEY_CALLER_QN: "alpha.app",
        cs.KEY_REL: cs.RelationshipType.IMPORTS.value,
        cs.KEY_TARGET_LABEL: target_label,
        cs.KEY_TARGET_QN: target_qn,
    }


@pytest.mark.parametrize("dialect", [DIALECT_MEMGRAPH, DIALECT_NEO4J])
def test_the_module_path_index_is_created(dialect: str) -> None:
    ingestor, conn = _ingestor(dialect)
    ingestor.ensure_constraints()
    assert get_dialect(dialect).create_index("Module", "path") in conn.log


def test_the_capture_returns_no_other_projects_edges(
    twins: tuple[_StatefulIngestor, GraphUpdater],
) -> None:
    _store, alpha = twins
    rows = alpha._capture_inbound_edges(["util.py"])
    assert rows
    assert not [r for r in rows if not str(r[cs.KEY_TARGET_QN]).startswith("alpha.")]


def test_another_projects_module_is_not_restored(
    twins: tuple[_StatefulIngestor, GraphUpdater],
) -> None:
    _store, alpha = twins
    assert alpha._restorable_edge(_row(cs.NodeLabel.MODULE.value, "beta.util")) is None


# Negative: what must not change.


def test_the_capture_still_returns_this_projects_edges(
    twins: tuple[_StatefulIngestor, GraphUpdater],
) -> None:
    _store, alpha = twins
    captured = {
        (r[cs.KEY_CALLER_QN], r[cs.KEY_REL], r[cs.KEY_TARGET_QN])
        for r in alpha._capture_inbound_edges(["util.py"])
    }
    assert ("alpha.app.run", "CALLS", "alpha.util.helper") in captured
    assert ("alpha.app", "IMPORTS", "alpha.util") in captured


def test_this_projects_module_is_still_restored(
    twins: tuple[_StatefulIngestor, GraphUpdater],
) -> None:
    _store, alpha = twins
    assert alpha._restorable_edge(_row(cs.NodeLabel.MODULE.value, "alpha.util"))


def test_an_incremental_sync_leaves_the_other_project_alone(
    temp_repo: Path, twins: tuple[_StatefulIngestor, GraphUpdater]
) -> None:
    store, alpha = twins

    def beta_edges() -> set[tuple[object, ...]]:
        return {
            edge[:5]
            for edge in store.keyed_edges
            if str(edge[1]).startswith("beta") or str(edge[4]).startswith("beta")
        }

    before = beta_edges()
    (temp_repo / "alpha" / "util.py").write_text(
        "def helper():\n    return 2\n\n\ndef other():\n    return 3\n"
    )
    alpha.run()
    assert beta_edges() == before
