"""The isolated check's incomplete-run marker outlives any failed put-back (#1718).

The check marks the project incomplete before it writes and clears the mark
once the graph and the on-disk hash cache are both back. A cache left
holding the re-parse's hashes makes the next update skip the edited files,
so a failed cache restore must leave the project marked, exactly as a failed
graph restore does.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.structural_check import _measure_then_restore
from codebase_rag.structural_delta import StructuralDelta
from codebase_rag.types_defs import PropertyDict, ReingestReport, ResultRow


class _RecordingStore:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append(query)

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        pass

    def ensure_relationship_batch(self, *args: object, **kwargs: object) -> None:
        pass

    def flush_all(self) -> None:
        pass


def _run(
    root: Path, store: _RecordingStore, during: Callable[[], None] | None = None
) -> None:
    def measure(apply: Callable[[], ReingestReport]) -> StructuralDelta:
        if during is not None:
            during()
        return cast(StructuralDelta, {})

    _measure_then_restore(
        cast(GraphUpdater, SimpleNamespace(reingest_scope=())),
        store,
        "proj",
        root,
        lambda hook: cast(ReingestReport, {}),
        measure,
    )


def test_the_marker_is_cleared_once_graph_and_cache_are_back(tmp_path: Path) -> None:
    store = _RecordingStore()
    (tmp_path / cs.HASH_CACHE_FILENAME).write_text("{}")

    _run(tmp_path, store)

    assert store.writes == [
        cq.CYPHER_MARK_PROJECT_INCOMPLETE,
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE,
    ]


def test_a_failed_cache_restore_leaves_the_project_marked(tmp_path: Path) -> None:
    store = _RecordingStore()
    cache = tmp_path / cs.HASH_CACHE_FILENAME
    cache.write_text("{}")

    def block_the_cache() -> None:
        # A directory where the cache file was: writing it back fails.
        cache.unlink()
        cache.mkdir()

    with pytest.raises(OSError):
        _run(tmp_path, store, during=block_the_cache)

    assert store.writes == [cq.CYPHER_MARK_PROJECT_INCOMPLETE]
