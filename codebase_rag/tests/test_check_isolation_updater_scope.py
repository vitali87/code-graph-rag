from __future__ import annotations

from pathlib import Path

from check_isolation_helpers import (
    FIXTURE,
    PROJECT,
    _write,
)

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# --- the updater's side of the contract ---------------------------------------


def test_reingest_names_its_scope_before_the_write_hook_runs(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The capture has to cover exactly what the re-ingest deletes: the
    changed files plus the dependents it re-parses, known only inside the
    prologue. The updater publishes that set right before `before_write`."""
    root, store = indexed
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("helper", "assist"))
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    assert updater.reingest_scope == ()
    seen: list[tuple[str, ...]] = []

    updater.reingest(
        ["pkg/util.py"], before_write=lambda: seen.append(updater.reingest_scope)
    )

    assert seen == [("pkg/app.py", "pkg/util.py")]
