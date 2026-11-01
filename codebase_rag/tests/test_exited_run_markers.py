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
    exited_runs,
    retire_exited_runs,
    run_may_be_writing,
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
        assert query == cq.CYPHER_WRITING_RUN_OWNERS, query
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


@pytest.mark.parametrize(
    ("rows", "writing"),
    [
        ([], False),
        ([{cs.KEY_RUN_INCOMPLETE: True, cs.KEY_WRITING: False}], False),
        ([{cs.KEY_RUN_INCOMPLETE: True, cs.KEY_WRITING: True}], True),
        # A marker from before the phase existed reads as writing.
        ([{cs.KEY_RUN_INCOMPLETE: True}], True),
    ],
    ids=["none", "prologue", "writing", "legacy"],
)
def test_a_run_may_be_writing_reads_as_the_edit_guard_does(
    rows: list[ResultRow], writing: bool
) -> None:
    ingestor = MagicMock()
    ingestor.fetch_all.return_value = rows

    assert run_may_be_writing(ingestor, "gone") is writing
    ingestor.fetch_all.assert_called_once_with(
        cq.CYPHER_PROJECT_IS_INCOMPLETE, {cs.KEY_PROJECT_NAME: "gone"}
    )


def test_cannot_tell_is_not_no() -> None:
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = RuntimeError("store down")

    with pytest.raises(RuntimeError):
        run_may_be_writing(ingestor, "gone")
