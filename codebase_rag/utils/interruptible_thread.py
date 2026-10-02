"""Run blocking work in a worker thread that Ctrl+C can stop.

`asyncio.to_thread` keeps the event loop, and with it the spinner and the
Shift+Tab listener, alive while the work runs. But SIGINT only ever reaches
the main thread: Ctrl+C cancelled the awaiting task while the worker ran on to
the end, and the interpreter waited for it at exit, so `cgr start` looked
frozen for as long as the rest of its sync took.

Here the cancellation is handed to the worker as a `KeyboardInterrupt`, so the
work unwinds through its own handlers exactly as it would on the main thread
(a sync commits what it may and keeps its incomplete-run marker otherwise),
and the awaiting side waits for that before the cancellation carries on.
"""

import asyncio
import contextvars
import ctypes
import threading
from collections.abc import Callable


def _set_async_exc(ident: int, exc: type[BaseException] | None) -> None:
    # CPython raises `exc` in that thread at its next bytecode boundary; None
    # (a NULL pointer) withdraws one that has not been raised yet.
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(ident), None if exc is None else ctypes.py_object(exc)
    )


class _Worker:
    """One task, and the only way to interrupt it.

    The lock orders the two threads: the interrupt is delivered only while the
    task runs, and the worker withdraws one still pending before it returns
    to the executor, whose own code must never see it.
    """

    def __init__(self, task: Callable[[], None]) -> None:
        self._task = task
        self._lock = threading.Lock()
        self._ident: int | None = None
        self._stop_requested = False

    def run(self) -> None:
        try:
            with self._lock:
                if self._stop_requested:
                    return
                self._ident = threading.get_ident()
            try:
                self._task()
            finally:
                with self._lock:
                    if self._ident is not None:
                        _set_async_exc(self._ident, None)
                    self._ident = None
        except KeyboardInterrupt:
            # A worker thread never receives SIGINT itself, so this is the
            # interrupt `interrupt` delivered; the awaiting side re-raises its
            # own cancellation. Anything else is not ours to swallow.
            if not self._stop_requested:
                raise

    def interrupt(self) -> None:
        with self._lock:
            if self._stop_requested:
                return
            self._stop_requested = True
            if self._ident is not None:
                _set_async_exc(self._ident, KeyboardInterrupt)


async def run_in_interruptible_thread(task: Callable[[], None]) -> None:
    """`asyncio.to_thread(task)`, except that cancelling it stops `task`."""
    worker = _Worker(task)
    context = contextvars.copy_context()
    future = asyncio.get_running_loop().run_in_executor(None, context.run, worker.run)
    try:
        # Shielded: a cancelled executor future stops being awaitable while
        # its thread runs on, and the wait below needs it.
        await asyncio.shield(future)
    except asyncio.CancelledError:
        worker.interrupt()
        await asyncio.gather(future, return_exceptions=True)
        raise
