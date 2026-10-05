"""Issue #2392: the known-definitions read binds every parameter it names.

`CYPHER_DELTA_DEFINITIONS` gained `$longer_project_prefixes`, and two of its
three callers were updated. `GraphUpdater._known_simple_names_by_path` was
not, so Memgraph rejected the read on every incremental sync, logged three
ERROR lines and a WARNING, and the method fell back to "every name known",
which silently disabled the gained-definitions narrowing of issue #1568.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import logs as ls
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow
from evals.cgr_graph import _StatefulIngestor

PARAM = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class _RecordingIngestor(_StatefulIngestor):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[tuple[str, PropertyDict | None]] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        self.reads.append((query, params))
        return super().fetch_all(query, params)


def _updater(store: _StatefulIngestor, repo: Path, project: str) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


def _indexed(tmp_path: Path, store: _StatefulIngestor, project: str) -> Path:
    repo = tmp_path / project
    repo.mkdir()
    (repo / "mod.py").write_text("def a():\n    return 1\n\nclass Box:\n    pass\n")
    _updater(store, repo, project).run(force=True)
    return repo


def test_known_names_come_from_the_graph_not_the_failure_fallback(
    tmp_path: Path,
) -> None:
    store = _StatefulIngestor()
    repo = _indexed(tmp_path, store, "proj")

    known = _updater(store, repo, "proj")._known_simple_names_by_path(["mod.py"])

    assert known == {"mod.py": {"a", "Box"}}


def test_the_read_binds_every_parameter_its_query_names(tmp_path: Path) -> None:
    store = _RecordingIngestor()
    repo = _indexed(tmp_path, store, "proj")
    store.reads.clear()

    _updater(store, repo, "proj")._known_simple_names_by_path(["mod.py"])

    [params] = [p for q, p in store.reads if q == cq.CYPHER_DELTA_DEFINITIONS]
    assert set(PARAM.findall(cq.CYPHER_DELTA_DEFINITIONS)) <= set(params or {})


def test_a_longer_projects_definitions_are_not_claimed(tmp_path: Path) -> None:
    store = _StatefulIngestor()
    repo = _indexed(tmp_path, store, "proj")
    nested = tmp_path / "proj_sub_src"
    nested.mkdir()
    (nested / "mod.py").write_text("def only_in_sub():\n    return 2\n")
    _updater(store, nested, "proj.sub").run(force=True)

    known = _updater(store, repo, "proj")._known_simple_names_by_path(["mod.py"])

    assert "only_in_sub" not in known.get("mod.py", set())
    assert known == {"mod.py": {"a", "Box"}}


def test_the_graph_double_refuses_an_unbound_parameter() -> None:
    store = _StatefulIngestor()

    with pytest.raises(ValueError, match="longer_project_prefixes"):
        store.fetch_all(
            cq.CYPHER_DELTA_DEFINITIONS,
            {cs.KEY_PROJECT_PREFIX: "proj.", cs.CYPHER_PARAM_PATHS: ["mod.py"]},
        )


def test_an_incremental_sync_logs_no_failed_known_definitions_read(
    tmp_path: Path,
) -> None:
    store = _StatefulIngestor()
    repo = _indexed(tmp_path, store, "proj")
    (repo / "mod.py").write_text(
        "def a():\n    return 2\n\ndef b():\n    return a()\n\nclass Box:\n    pass\n"
    )
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        _updater(store, repo, "proj").run(force=False)
    finally:
        logger.remove(sink)

    failed = ls.PRUNE_QUERY_FAILED.format(label="known definitions")
    assert not [w for w in warnings if failed in w]


def test_a_reingest_logs_no_failed_known_definitions_read(tmp_path: Path) -> None:
    # `cgr check` and the MCP edit tools go through `reingest`, which runs
    # the same known-definitions read (issue #2392, comment).
    store = _StatefulIngestor()
    repo = _indexed(tmp_path, store, "proj")
    (repo / "mod.py").write_text(
        "def a():\n    return 2\n\ndef b():\n    return a()\n\nclass Box:\n    pass\n"
    )
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        _updater(store, repo, "proj").reingest([repo / "mod.py"])
    finally:
        logger.remove(sink)

    failed = ls.PRUNE_QUERY_FAILED.format(label="known definitions")
    assert not [w for w in warnings if failed in w]
