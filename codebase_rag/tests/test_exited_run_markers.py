"""Markers of runs whose process exited, for a deleted or pruned project (#2479).

A marker sits on its own node, out of a delete's reach. One whose run died
after its first write kept the project in `cgr status` as interrupted once
the project was gone, and kept `cgr prune` from removing a project whose
sync died with its checkout. Only a run this host can prove exited goes; a
run elsewhere may still be writing (#1709).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.sync_guard import (
    StrandedRun,
    WritingRuns,
    exited_runs,
    retire_exited_runs,
    take_over_runs,
    writing_runs,
)
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.process_owner import process_host


@pytest.fixture(autouse=True)
def _identified_process_table() -> Iterator[None]:
    with patch("codebase_rag.sync_guard.process_table_identified", return_value=True):
        yield


def _gone_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _owner(run_id: str, pid: int, host: str | None = None) -> ResultRow:
    return {
        cs.KEY_RUN_ID: run_id,
        cs.KEY_OWNER_HOST: process_host() if host is None else host,
        cs.KEY_OWNER_PID: pid,
    }


class Store:
    def __init__(self, owners: list[ResultRow]) -> None:
        self.owners = owners
        self.writes: list[tuple[str, PropertyDict]] = []
        self.ingestor = MagicMock()
        self.ingestor.fetch_all.side_effect = self.fetch_all
        self.ingestor.execute_write.side_effect = self.execute_write

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        assert query == cq.CYPHER_WRITING_RUNS, query
        assert params == {cs.KEY_PROJECT_NAME: "gone"}
        return self.owners

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append((query, dict(params or {})))


def test_only_a_run_whose_process_exited_here_counts() -> None:
    dead = _gone_pid()
    store = Store(
        [
            _owner("mcp-dead", dead),
            _owner("mcp-alive", os.getppid()),
            _owner("elsewhere", dead, host="another-host"),
        ]
    )

    assert exited_runs(store.ingestor, "gone") == [
        StrandedRun("mcp-dead", process_host(), dead)
    ]


def test_retiring_deletes_only_while_the_marker_names_that_process() -> None:
    dead = _gone_pid()
    store = Store([_owner("cli-sync", dead)])

    retire_exited_runs(store.ingestor, "gone")

    assert store.writes == [
        (
            cq.CYPHER_RETIRE_STRANDED_RUN,
            {
                cs.KEY_PROJECT_NAME: "gone",
                cs.KEY_RUN_ID: "cli-sync",
                cs.KEY_OWNER_HOST: process_host(),
                cs.KEY_OWNER_PID: dead,
            },
        )
    ]


def test_an_unreadable_store_retires_nothing_and_does_not_raise() -> None:
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = RuntimeError("store down")

    retire_exited_runs(ingestor, "gone")

    ingestor.execute_write.assert_not_called()


def test_a_failed_retire_does_not_raise() -> None:
    store = Store([_owner("mcp-dead", _gone_pid())])
    store.ingestor.execute_write.side_effect = RuntimeError("connection reset")

    retire_exited_runs(store.ingestor, "gone")


def test_a_marker_that_names_no_process_is_never_exited() -> None:
    # From before markers named their process: it may still be writing.
    store = Store(
        [{cs.KEY_RUN_ID: None, cs.KEY_OWNER_HOST: None, cs.KEY_OWNER_PID: None}]
    )

    assert writing_runs(store.ingestor, "gone", own_run_id="prune-1") == WritingRuns(
        live=True, exited=[]
    )
    assert exited_runs(store.ingestor, "gone") == []


def test_writing_runs_split_live_from_exited_and_skip_the_callers_own() -> None:
    dead = _gone_pid()
    store = Store(
        [
            _owner("prune-1", os.getpid()),
            _owner("mcp-dead", dead),
            _owner("elsewhere", dead, host="another-host"),
        ]
    )

    assert writing_runs(store.ingestor, "gone", own_run_id="prune-1") == WritingRuns(
        live=True, exited=[StrandedRun("mcp-dead", process_host(), dead)]
    )


def test_writing_runs_with_only_its_own_marker_finds_no_writer() -> None:
    store = Store([_owner("prune-1", os.getpid())])

    assert writing_runs(store.ingestor, "gone", own_run_id="prune-1") == WritingRuns(
        live=False, exited=[]
    )


def test_cannot_tell_is_not_no() -> None:
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = RuntimeError("store down")

    with pytest.raises(RuntimeError):
        writing_runs(ingestor, "gone", own_run_id="prune-1")


def test_taking_over_marks_before_it_retires() -> None:
    dead = _gone_pid()
    store = Store([])
    run = StrandedRun("mcp-dead", process_host(), dead)

    take_over_runs(store.ingestor, "gone", "prune-1", [run])

    (mark, mark_params), (retire, retire_params) = store.writes
    assert mark == cq.CYPHER_MARK_RUN_INCOMPLETE
    assert mark_params[cs.KEY_RUN_ID] == "prune-1"
    assert mark_params[cs.KEY_WRITING] is True
    assert mark_params[cs.KEY_OWNER_PID] == os.getpid()
    assert retire == cq.CYPHER_RETIRE_STRANDED_RUN
    assert retire_params[cs.KEY_RUN_ID] == "mcp-dead"


def test_nothing_to_take_over_writes_nothing() -> None:
    store = Store([])

    take_over_runs(store.ingestor, "gone", "prune-1", [])

    assert store.writes == []


def test_a_failed_mark_retires_nothing() -> None:
    store = Store([])
    store.ingestor.execute_write.side_effect = RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        take_over_runs(
            store.ingestor,
            "gone",
            "prune-1",
            [StrandedRun("mcp-dead", process_host(), _gone_pid())],
        )

    assert store.ingestor.execute_write.call_count == 1
