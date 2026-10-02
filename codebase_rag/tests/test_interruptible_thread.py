"""`run_in_interruptible_thread` hands a cancellation to its worker thread.

SIGINT only reaches the main thread, so work in a worker thread ran on after
Ctrl+C. The interrupt is delivered into the worker instead, and it must reach
only the task it was meant for: never a later job on the same executor thread,
never twice, and never in place of an interrupt the task raised itself.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from codebase_rag.utils.interruptible_thread import (
    _Worker,
    run_in_interruptible_thread,
)

_RUNS_FOR = 10.0


def _spin(started: threading.Event) -> None:
    started.set()
    deadline = time.monotonic() + _RUNS_FOR
    while time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.mark.asyncio
async def test_cancelling_the_wait_interrupts_the_task() -> None:
    started = threading.Event()
    interrupted = threading.Event()

    def task() -> None:
        try:
            _spin(started)
        except KeyboardInterrupt:
            interrupted.set()
            raise

    waiting = asyncio.create_task(run_in_interruptible_thread(task))
    await asyncio.to_thread(started.wait, 5.0)
    cancelled_at = time.monotonic()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert interrupted.is_set()
    assert time.monotonic() - cancelled_at < _RUNS_FOR / 3


@pytest.mark.asyncio
async def test_a_task_left_alone_runs_to_completion() -> None:
    task = MagicMock()

    await run_in_interruptible_thread(task)

    task.assert_called_once_with()


@pytest.mark.asyncio
async def test_a_task_error_reaches_the_awaiter() -> None:
    def fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_in_interruptible_thread(fail)


def test_an_interrupt_the_task_raised_itself_is_not_swallowed() -> None:
    # Only the interrupt this module delivered is absorbed; one the task
    # raised with nobody asking must still reach the caller.
    def task() -> None:
        raise KeyboardInterrupt

    worker = _Worker(task)
    with pytest.raises(KeyboardInterrupt):
        worker.run()


def test_a_second_interrupt_does_not_cut_the_cleanup_short() -> None:
    # A sync interrupted once is committing or flushing in its handler; a
    # second delivery would abort exactly that.
    started = threading.Event()
    cleaning = threading.Event()
    cleaned = threading.Event()

    def task() -> None:
        try:
            _spin(started)
        except KeyboardInterrupt:
            cleaning.set()
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                time.sleep(0.01)
            cleaned.set()
            raise

    worker = _Worker(task)
    thread = threading.Thread(target=worker.run)
    thread.start()
    assert started.wait(5.0)
    worker.interrupt()
    assert cleaning.wait(5.0)

    worker.interrupt()
    thread.join(5.0)

    assert cleaned.is_set()


def test_an_interrupt_after_the_task_finished_is_never_delivered() -> None:
    # Run on this thread, so a stray interrupt would surface right here
    # rather than in some later executor job.
    worker = _Worker(MagicMock())
    worker.run()

    worker.interrupt()

    assert sum(range(100_000)) > 0


def test_an_interrupt_before_the_task_started_skips_it() -> None:
    task = MagicMock()
    worker = _Worker(task)

    worker.interrupt()
    worker.run()

    task.assert_not_called()
