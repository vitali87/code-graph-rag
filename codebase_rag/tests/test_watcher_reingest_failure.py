"""A watcher must not reuse its updater after a re-ingest died mid-run (#1681).

`GraphUpdater.reingest` deletes the affected subtrees before rebuilding them, so
a failure between those two steps leaves the graph missing definitions the
updater's in-memory registry still describes. The watcher caught only
`ValueError` -- the refusals raised before any write -- so any other exception
escaped the callback with the updater retained, and every later event resolved
calls against that registry over a partial graph.

The MCP tool closed the same class in #1538 by dropping its retained updater and
refusing until `update_repository` completes. A watcher cannot refuse for ever,
so it recovers instead: the next change re-indexes the whole repository before
any scoped work.

The controls carry the weight here. "Never reuse the updater" is satisfied by a
watcher that dies on the first error, and "always rebuild" by one that re-indexes
on every event -- both would pass a bare regression test and both would be worse
than the bug.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import EventType
from codebase_rag.graph_updater import ReingestAborted
from realtime_updater import CodeChangeEventHandler


class _Event:
    """The attributes `dispatch` reads off a watchdog event."""

    def __init__(self, path: str, event_type: str = EventType.MODIFIED) -> None:
        self.src_path = path
        self.event_type = event_type
        self.is_directory = False


@pytest.fixture
def updater() -> MagicMock:
    mock = MagicMock()
    mock.repo_path = Path("/repo")
    return mock


@pytest.fixture
def handler(updater: MagicMock) -> CodeChangeEventHandler:
    # debounce off: dispatch runs the change inline, so the assertions below
    # observe the handler rather than a timer.
    return CodeChangeEventHandler(updater=updater, debounce_seconds=0)


class TestReingestFailedMidRun:
    def test_the_failure_does_not_escape_the_callback(
        self, handler: CodeChangeEventHandler, updater: MagicMock
    ) -> None:
        # An exception out of a watchdog callback kills the handler thread
        # silently; the watcher stays "running" and stops updating anything.
        updater.reingest.side_effect = RuntimeError("died after the delete")
        handler.dispatch(_Event("/repo/a.py"))

    def test_the_next_change_re_indexes_before_any_scoped_work(
        self, handler: CodeChangeEventHandler, updater: MagicMock
    ) -> None:
        updater.reingest.side_effect = RuntimeError("died after the delete")
        handler.dispatch(_Event("/repo/a.py"))
        assert not updater.run.called, "the failing event itself must not re-index"

        updater.reingest.side_effect = None
        handler.dispatch(_Event("/repo/b.py"))
        assert updater.run.called, (
            "the next change must restore a whole graph before resolving against it"
        )

    def test_the_rebuild_happens_once_not_on_every_later_change(
        self, handler: CodeChangeEventHandler, updater: MagicMock
    ) -> None:
        # The control against over-correcting. A watcher that re-indexes on
        # every event satisfies the test above and makes the scoped path
        # pointless.
        updater.reingest.side_effect = RuntimeError("died after the delete")
        handler.dispatch(_Event("/repo/a.py"))
        updater.reingest.side_effect = None
        handler.dispatch(_Event("/repo/b.py"))
        handler.dispatch(_Event("/repo/c.py"))
        assert updater.run.call_count == 1, (
            f"expected one recovery re-index, got {updater.run.call_count}"
        )


class TestTheRecoveryItself:
    def test_the_rebuild_is_forced(
        self, handler: CodeChangeEventHandler, updater: MagicMock
    ) -> None:
        # An incremental run skips files whose hashes are unchanged, and after a
        # re-ingest that deleted subtrees without rebuilding them the FILES are
        # unchanged. A plain run() would skip exactly the files whose nodes are
        # missing and report success over a still-partial graph.
        updater.reingest.side_effect = RuntimeError("died after the delete")
        handler.dispatch(_Event("/repo/a.py"))
        updater.reingest.side_effect = None
        handler.dispatch(_Event("/repo/b.py"))

        updater.run.assert_called_once_with(force=True)

    def test_a_failed_rebuild_does_not_end_the_dispatcher(
        self, handler: CodeChangeEventHandler, updater: MagicMock
    ) -> None:
        # This runs from a watchdog callback: an exception here kills the
        # dispatcher thread and the watcher goes silently deaf, which is the
        # failure this recovery exists to prevent, one level up.
        updater.reingest.side_effect = RuntimeError("died after the delete")
        handler.dispatch(_Event("/repo/a.py"))
        updater.reingest.side_effect = None
        updater.run.side_effect = RuntimeError("rebuild failed too")

        handler.dispatch(_Event("/repo/b.py"))

    def test_a_failed_rebuild_retries_and_skips_the_scoped_work(
        self, handler: CodeChangeEventHandler, updater: MagicMock
    ) -> None:
        updater.reingest.side_effect = RuntimeError("died after the delete")
        handler.dispatch(_Event("/repo/a.py"))
        updater.reingest.reset_mock()
        updater.reingest.side_effect = None
        updater.run.side_effect = RuntimeError("rebuild failed too")

        handler.dispatch(_Event("/repo/b.py"))
        # Resolving this change against a graph known to be partial would
        # compound the damage, so the scoped work is skipped entirely.
        assert not updater.reingest.called

        updater.run.side_effect = None
        handler.dispatch(_Event("/repo/c.py"))
        assert updater.run.call_count == 2, "the next change must retry the rebuild"
        assert updater.reingest.called, "and then resume scoped work"


class TestRefusalsAreNotFailures:
    @pytest.mark.parametrize(
        "error",
        [ValueError("outside the repository"), ReingestAborted("still reading")],
        ids=["refusal", "abort"],
    )
    def test_a_refusal_leaves_the_updater_usable(
        self, handler: CodeChangeEventHandler, updater: MagicMock, error: Exception
    ) -> None:
        # Both are raised before anything was written -- a refusal while the
        # paths are split, an abort while the call was still READING the graph.
        # Treating them as damage would re-index the repository every time an
        # agent touched a path outside it.
        updater.reingest.side_effect = error
        handler.dispatch(_Event("/repo/a.py"))
        updater.reingest.side_effect = None
        handler.dispatch(_Event("/repo/b.py"))
        assert not updater.run.called, (
            "a refusal wrote nothing, so no recovery re-index is warranted"
        )

    def test_a_clean_run_never_re_indexes(
        self, handler: CodeChangeEventHandler, updater: MagicMock
    ) -> None:
        handler.dispatch(_Event("/repo/a.py"))
        handler.dispatch(_Event("/repo/b.py"))
        assert not updater.run.called
        assert updater.reingest.call_count == 2
