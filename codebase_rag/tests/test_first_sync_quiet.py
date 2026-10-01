"""Issue #2404: a new user's first sync logs no false warning or notice.

The very first `--update-graph` on a fresh stack logged a WARNING ("Could not
read module paths from the graph") and an upgrade notice ("No recorded
exclusion set ... Expect this exactly once per existing index"). The seeded
module map prune read every Module of the whole shared graph and called an
empty answer unreadable, which on a brand-new graph it simply is not; and the
notice spoke of an existing index that did not exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow
from evals.cgr_graph import _StatefulIngestor


class _Buffered(_StatefulIngestor):
    """Reads see only flushed writes, as Memgraph's do.

    The suite's store is write-through, so a node the sync just buffered is
    readable at once and the first sync's module read never comes back
    empty: the case this issue is about cannot occur without buffering.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[tuple[str, PropertyDict]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self._pending.append((label, properties))

    def flush_all(self) -> None:
        pending, self._pending = self._pending, []
        for label, properties in pending:
            super().ensure_node_batch(label, properties)
        super().flush_all()


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "core.py").write_text("def a():\n    return 1\n")
    return repo


def _sync(store: _StatefulIngestor, repo: Path, name: str) -> list[str]:
    parsers, queries = load_parsers()
    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO", format="{level}|{message}")
    try:
        GraphUpdater(
            ingestor=store,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            project_name=name,
        ).run(force=False)
    finally:
        logger.remove(sink)
    return messages


def test_a_first_sync_on_an_empty_graph_logs_no_seed_prune_warning(
    tmp_path: Path,
) -> None:
    messages = _sync(_Buffered(), _repo(tmp_path, "myrepo"), "myrepo")

    assert not [m for m in messages if ls.SEED_PRUNE_NO_VERDICT in m]


def test_a_first_sync_says_nothing_about_an_existing_index(tmp_path: Path) -> None:
    messages = _sync(_StatefulIngestor(), _repo(tmp_path, "myrepo"), "myrepo")

    assert not [m for m in messages if ls.EXCLUSION_STATE_MISSING in m]


def test_an_index_built_before_the_stamp_still_gets_the_notice(
    tmp_path: Path,
) -> None:
    # Negative: the upgrade path the notice exists for keeps it.
    store = _StatefulIngestor()
    repo = _repo(tmp_path, "myrepo")
    _sync(store, repo, "myrepo")
    (repo / cs.EXCLUSION_STATE_FILENAME).unlink()
    (repo / "pkg" / "core.py").write_text("def a():\n    return 2\n")

    messages = _sync(store, repo, "myrepo")

    assert [m for m in messages if ls.EXCLUSION_STATE_MISSING in m]


def test_the_seed_prune_reads_only_this_projects_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The prune pulled every module of every project in the shared graph on
    # each sync, so a three-file repo synced ten times slower beside others.
    store = _StatefulIngestor()
    _sync(store, _repo(tmp_path, "other"), "other")
    repo = _repo(tmp_path, "myrepo")
    _sync(store, repo, "myrepo")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="myrepo",
    )
    module_map = updater.factory.definition_processor.module_qn_to_file_path
    module_map.update(
        {"myrepo.pkg.core": "pkg/core.py", "myrepo.pkg.gone": "pkg/gone.py"}
    )
    reads: list[tuple[str, list[ResultRow]]] = []
    original = store.fetch_all

    def _recording(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        rows = original(query, params)
        reads.append((query, rows))
        return rows

    monkeypatch.setattr(store, "fetch_all", _recording)
    updater._prune_stale_seeded_module_qns()
    monkeypatch.undo()

    qns = [
        str(row[cs.KEY_QUALIFIED_NAME])
        for _q, rows in reads
        for row in rows
        if cs.KEY_QUALIFIED_NAME in row
    ]
    assert qns
    assert all(qn.startswith("myrepo.") for qn in qns), qns
    # Negative: the prune still does its job on the narrower read.
    assert "myrepo.pkg.gone" not in module_map
    assert "myrepo.pkg.core" in module_map


def test_a_longer_named_project_is_not_read_as_this_ones(tmp_path: Path) -> None:
    # Negative: `myrepo.` also prefixes `myrepo.sub`'s modules; the read keeps
    # the longest-prefix ownership rule the other rehydration reads use.
    store = _StatefulIngestor()
    _sync(store, _repo(tmp_path, "sub"), "myrepo.sub")
    repo = _repo(tmp_path, "myrepo")
    _sync(store, repo, "myrepo")
    (repo / "pkg" / "core.py").write_text("def a():\n    return 2\n")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="myrepo",
    )

    updater.run(force=False)

    seeded = updater.factory.definition_processor.module_qn_to_file_path
    assert not [qn for qn in seeded if qn.startswith("myrepo.sub.")], seeded


def test_an_unreadable_module_read_on_a_seeded_map_still_warns(
    tmp_path: Path,
) -> None:
    # Negative: once the map holds modules this run did not parse, an empty
    # read is still "no verdict", and still said so.
    store = _Buffered()
    repo = _repo(tmp_path, "myrepo")
    (repo / "pkg" / "extra.py").write_text("def b():\n    return 2\n")
    _sync(store, repo, "myrepo")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="myrepo",
    )
    updater.factory.definition_processor.module_qn_to_file_path.update(
        {"myrepo.pkg.core": "pkg/core.py", "myrepo.pkg.extra": "pkg/extra.py"}
    )
    store.nodes.clear()
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        updater._prune_stale_seeded_module_qns()
    finally:
        logger.remove(sink)

    assert [w for w in warnings if ls.SEED_PRUNE_NO_VERDICT in w]
