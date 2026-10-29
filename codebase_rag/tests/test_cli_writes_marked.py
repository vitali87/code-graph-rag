"""Every CLI writer re-ingests between the `:IncompleteRun` marker's mark and
clear (#2219).

`cgr start` and the watcher held the CLI sync's marker down while they wrote,
but `cgr rename`, `cgr edit undo` and the chat's graph refresh called
`reingest` raw. A process that died part-way through one of them left a
partial graph that `cgr status` and the MCP server took for whole.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.editing.cli import _reingest_restored
from codebase_rag.sync.guard import (
    UnfinishedSyncError,
    WriteMarker,
    marked_reingest,
    sync_owner,
)
from codebase_rag.tools.graph_refresh import GraphRefresher
from codebase_rag.types_defs import ReingestReport

_PROJECT = "proj"
_REPORT = ReingestReport(
    reparsed=("a.py",), affected=(), removed=(), skipped=(), elapsed_ms=0.0
)


class _Connection:
    """The graph a CLI writer marks on: answers the marker and project reads
    and records every write."""

    def __init__(self, marker_down: bool, projects: tuple[str, ...]) -> None:
        self.marker_down = marker_down
        self.projects = projects
        self.writes: list[str] = []

    def fetch_all(self, query: str, params: object = None) -> list[dict[str, object]]:
        if query == cq.CYPHER_CLI_SYNC_MARKER_OWNER:
            return [{"owner": "x"}] if self.marker_down else []
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{"name": name} for name in self.projects]
        return []

    def execute_write(self, query: str, params: object = None) -> None:
        self.writes.append(query)

    def list_projects(self) -> list[str]:
        return list(self.projects)


def _connection(
    marker_down: bool = False, projects: tuple[str, ...] = ()
) -> _Connection:
    return _Connection(marker_down, projects)


def _writes(connection: _Connection) -> list[str]:
    return connection.writes


class _Updater:
    """A re-ingest that writes after its prologue, as GraphUpdater's does."""

    def __init__(self, fail_after_write: bool = False) -> None:
        self.fail_after_write = fail_after_write
        self.project_name = _PROJECT
        self.committed = True

    def reingest(
        self, paths: list[str], before_write: Callable[[], None] | None = None
    ) -> ReingestReport:
        if before_write is not None:
            before_write()
        if self.fail_after_write:
            raise RuntimeError("died after the first delete")
        return _REPORT

    def run(self, force: bool = False) -> None:
        pass


def test_a_reingest_is_marked_then_cleared() -> None:
    connection = _connection()
    with sync_owner() as owner:
        report = marked_reingest(
            _Updater(), WriteMarker(connection, _PROJECT, owner, None)
        )(["a.py"])
    assert report == _REPORT
    assert _writes(connection) == [
        cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE,
        cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER,
    ]


def test_a_reingest_that_dies_after_writing_leaves_the_marker_down() -> None:
    connection = _connection()
    with sync_owner() as owner, pytest.raises(RuntimeError):
        marked_reingest(
            _Updater(fail_after_write=True),
            WriteMarker(connection, _PROJECT, owner, None),
        )(["a.py"])
    assert _writes(connection) == [cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE]


def test_a_reingest_over_an_unfinished_sync_is_refused_before_it_writes() -> None:
    connection = _connection(marker_down=True)
    with sync_owner() as owner, pytest.raises(UnfinishedSyncError):
        marked_reingest(_Updater(), WriteMarker(connection, _PROJECT, owner, None))(
            ["a.py"]
        )
    assert _writes(connection) == []


def test_the_undo_reingest_is_marked(tmp_path: Path) -> None:
    connection = _connection(projects=(_PROJECT,))
    with (
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=_Updater()),
        patch("codebase_rag.parser_loader.load_parsers", return_value=({}, {})),
    ):
        assert _reingest_restored(connection, tmp_path, _PROJECT, ["a.py"]) is True
    assert _writes(connection) == [
        cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE,
        cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER,
    ]


def test_the_chat_refresh_is_marked(tmp_path: Path) -> None:
    connection = _connection(projects=(_PROJECT,))
    refresher = GraphRefresher(connection, tmp_path, _PROJECT)
    refresher._updater = _Updater()  # ty: ignore[invalid-assignment]
    refresher.refresh(["a.py"])
    assert _writes(connection) == [
        cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE,
        cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER,
    ]


def test_the_chat_refresh_rebuilds_over_an_unfinished_sync(tmp_path: Path) -> None:
    connection = _connection(marker_down=True, projects=(_PROJECT,))
    updater = _Updater()
    runs: list[bool] = []
    updater.run = lambda force=False: runs.append(force)  # ty: ignore[invalid-assignment]
    refresher = GraphRefresher(connection, tmp_path, _PROJECT)
    refresher._updater = updater  # ty: ignore[invalid-assignment]
    refresher.refresh(["a.py"])
    assert runs == [True]
