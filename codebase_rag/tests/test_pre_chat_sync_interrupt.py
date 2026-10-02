"""Ctrl+C during `cgr start`'s pre-chat sync has to stop the sync.

The sync ran through `asyncio.to_thread`, and SIGINT only reaches the main
thread: Ctrl+C cancelled the wait while the sync ran on to the end, and the
process sat there until it did.
"""

import asyncio
import threading
import time

import pytest

from codebase_rag.main import _run_pre_chat_sync

_RUNS_FOR = 10.0


class _Sync:
    """Stands in for the sync: runs until interrupted, then cleans up."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.interrupted = False
        self.unwound = False

    def __call__(self) -> None:
        self.started.set()
        try:
            deadline = time.monotonic() + _RUNS_FOR
            while time.monotonic() < deadline:
                time.sleep(0.01)
        except KeyboardInterrupt:
            self.interrupted = True
            raise
        finally:
            # A real sync commits or keeps its incomplete marker here.
            time.sleep(0.05)
            self.unwound = True


@pytest.mark.asyncio
async def test_ctrl_c_stops_the_pre_chat_sync() -> None:
    sync = _Sync()
    waiting = asyncio.create_task(_run_pre_chat_sync(sync, "syncing"))
    await asyncio.to_thread(sync.started.wait, 5.0)

    cancelled_at = time.monotonic()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert sync.interrupted
    # Cancellation completes only after the sync has unwound, so nothing is
    # still writing to the graph once `cgr start` reports it stopped.
    assert sync.unwound
    assert time.monotonic() - cancelled_at < _RUNS_FOR / 3


@pytest.mark.asyncio
async def test_a_pre_chat_sync_left_alone_runs_to_completion() -> None:
    finished = threading.Event()

    await _run_pre_chat_sync(finished.set, "syncing")

    assert finished.is_set()
