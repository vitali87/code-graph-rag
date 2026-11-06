"""How long one statement may run.

A caller's timeout (`QUERY_TIMEOUT_S` for generated queries) only stops the
caller waiting; the statement would run on in its worker thread, holding the
connection. The engine therefore checks the deadline itself: SQLite through
a progress handler, which aborts the statement it is executing, and the
Python procedures between steps of their loops.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..constants.embedded_graph import EG_DEADLINE_VAR, EG_ERR_TIMEOUT


class QueryTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class Deadline:
    # A time.monotonic() reading, or None for no limit.
    at: float | None = None

    @classmethod
    def after(cls, seconds: float | None) -> Deadline:
        return cls(None if seconds is None else time.monotonic() + seconds)

    def passed(self) -> bool:
        return self.at is not None and time.monotonic() > self.at


_current: ContextVar[Deadline] = ContextVar(EG_DEADLINE_VAR, default=Deadline())


@contextmanager
def running_until(deadline: Deadline) -> Generator[None]:
    token = _current.set(deadline)
    try:
        yield
    finally:
        _current.reset(token)


def seconds_left() -> float | None:
    """What remains of the running statement's time (None: no limit).

    For code that keeps its own clock, as the Rust extension does: a
    monotonic reading means nothing outside this process's Python.
    """
    at = _current.get().at
    return None if at is None else at - time.monotonic()


def check_deadline() -> None:
    """Stop a long-running procedure once its statement's time is up."""
    if _current.get().passed():
        raise QueryTimeout(EG_ERR_TIMEOUT)
