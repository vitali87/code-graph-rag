"""The debouncer keeps the change a burst of events describes (issue #2430).

On Linux, watchdog's inotify backend reports one ordinary write as
`created, opened, modified, closed` (or `opened, modified, closed` for an
existing file), and a read as `opened, closed_no_write`. The debouncer used to
keep only the LAST event per path, so the timer handed `closed` to
`_process_change_locked`, which skips anything but created/modified/deleted:
with the default debounce no edit ever reached the graph.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger
from watchdog.events import (
    FileClosedEvent,
    FileClosedNoWriteEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileOpenedEvent,
    FileSystemEvent,
)

import realtime_updater
from codebase_rag import logs
from realtime_updater import CodeChangeEventHandler

DEBOUNCE_SECONDS = 5.0
MAX_WAIT_SECONDS = 30.0

EventFactory = Callable[[str], FileSystemEvent]

# The sequences watchdog 6 emits on Linux, as observed with InotifyObserver.
INOTIFY_NEW_FILE_WRITE: tuple[EventFactory, ...] = (
    FileCreatedEvent,
    FileOpenedEvent,
    FileModifiedEvent,
    FileClosedEvent,
)
INOTIFY_OVERWRITE: tuple[EventFactory, ...] = (
    FileOpenedEvent,
    FileModifiedEvent,
    FileClosedEvent,
)
INOTIFY_READ: tuple[EventFactory, ...] = (FileOpenedEvent, FileClosedNoWriteEvent)


class QueuedTimer:
    """A `threading.Timer` stand-in that fires only when the test says so."""

    def __init__(
        self,
        queue: list[QueuedTimer],
        interval: float,
        function: Callable[..., None],
        args: Sequence[str] = (),
    ) -> None:
        self._queue = queue
        self.interval = interval
        self.function = function
        self.args = tuple(args)
        self.daemon = False
        self.cancelled = False

    def start(self) -> None:
        self._queue.append(self)

    def cancel(self) -> None:
        self.cancelled = True


class Timers:
    def __init__(self) -> None:
        self.scheduled: list[QueuedTimer] = []

    def factory(
        self, interval: float, function: Callable[..., None], args: Sequence[str] = ()
    ) -> QueuedTimer:
        return QueuedTimer(self.scheduled, interval, function, args)

    def live(self) -> list[QueuedTimer]:
        return [t for t in self.scheduled if not t.cancelled]

    def fire_all(self) -> None:
        for timer in self.live():
            timer.cancelled = True
            timer.function(*timer.args)


@pytest.fixture
def timers() -> Timers:
    return Timers()


@pytest.fixture
def handler(mock_updater: MagicMock, timers: Timers) -> CodeChangeEventHandler:
    return CodeChangeEventHandler(
        mock_updater,
        debounce_seconds=DEBOUNCE_SECONDS,
        max_wait_seconds=MAX_WAIT_SECONDS,
        timer_factory=timers.factory,
    )


@pytest.fixture
def source(temp_repo: Path) -> Path:
    path = temp_repo / "m.py"
    path.write_text("def a():\n    return 1\n", encoding="utf-8")
    return path


@pytest.fixture
def info_messages() -> Generator[list[str], None, None]:
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]), level="INFO"
    )
    yield messages
    logger.remove(sink_id)


def _feed(
    handler: CodeChangeEventHandler,
    path: Path,
    sequence: Sequence[EventFactory],
) -> None:
    for make_event in sequence:
        handler.dispatch(make_event(str(path)))


class TestInotifyWriteReachesTheGraph:
    def test_new_file_write_sequence_is_reingested(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        _feed(handler, source, INOTIFY_NEW_FILE_WRITE)
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((source,))

    def test_overwrite_sequence_is_reingested(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        _feed(handler, source, INOTIFY_OVERWRITE)
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((source,))

    def test_closed_after_a_write_counts_as_modified(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        # A write through mmap raises no IN_MODIFY; IN_CLOSE_WRITE is then the
        # only sign the content changed.
        _feed(handler, source, (FileOpenedEvent, FileClosedEvent))
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((source,))

    def test_debounce_log_names_the_content_event_not_opened(
        self,
        handler: CodeChangeEventHandler,
        source: Path,
        info_messages: list[str],
    ) -> None:
        _feed(handler, source, INOTIFY_OVERWRITE)

        debouncing = logs.CHANGE_DEBOUNCING.format(
            event_type=FileModifiedEvent.event_type,
            name=source.name,
            debounce=DEBOUNCE_SECONDS,
        )
        assert debouncing in info_messages
        opened = logs.CHANGE_DEBOUNCING.format(
            event_type=FileOpenedEvent.event_type,
            name=source.name,
            debounce=DEBOUNCE_SECONDS,
        )
        assert opened not in info_messages

    def test_max_wait_flush_applies_the_write(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two saves straddling the max-wait deadline: the second one forces
        # the flush through the zero-delay timer instead of a fresh debounce.
        now = [1000.0]
        clock = SimpleNamespace(time=lambda: now[0])
        monkeypatch.setattr(realtime_updater, "time", clock)

        _feed(handler, source, INOTIFY_OVERWRITE)
        now[0] += MAX_WAIT_SECONDS
        _feed(handler, source, INOTIFY_OVERWRITE)

        assert [t.interval for t in timers.live()] == [0]
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((source,))
        assert handler.pending_events == {}
        assert handler.first_event_time == {}


class TestDeletionsInsideTheWindow:
    def test_delete_then_recreate_applies_the_new_content(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        handler.dispatch(FileDeletedEvent(str(source)))
        _feed(handler, source, INOTIFY_NEW_FILE_WRITE)
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((source,))

    def test_edit_then_delete_applies_as_a_delete(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        _feed(handler, source, INOTIFY_OVERWRITE)
        source.unlink()
        handler.dispatch(FileDeletedEvent(str(source)))
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((), deleted=(source,))

    def test_create_then_delete_applies_as_a_delete(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        temp_repo: Path,
    ) -> None:
        scratch = temp_repo / "scratch.py"
        scratch.write_text("x = 1\n", encoding="utf-8")
        _feed(handler, scratch, INOTIFY_NEW_FILE_WRITE)
        scratch.unlink()
        handler.dispatch(FileDeletedEvent(str(scratch)))
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((), deleted=(scratch,))

    @pytest.mark.parametrize(
        "trailing",
        [INOTIFY_READ, (FileClosedEvent,)],
        ids=["read", "writer-closes-unlinked-file"],
    )
    def test_a_later_open_or_close_does_not_hide_the_delete(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
        trailing: Sequence[EventFactory],
    ) -> None:
        source.unlink()
        handler.dispatch(FileDeletedEvent(str(source)))
        _feed(handler, source, trailing)
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((), deleted=(source,))


class TestReadsStayOutOfTheGraph:
    def test_read_only_sequence_triggers_no_reingest(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        _feed(handler, source, INOTIFY_READ)
        timers.fire_all()

        mock_updater.reingest.assert_not_called()

    def test_read_only_sequence_opens_no_debounce_window(
        self,
        handler: CodeChangeEventHandler,
        timers: Timers,
        source: Path,
        info_messages: list[str],
    ) -> None:
        _feed(handler, source, INOTIFY_READ)

        assert handler.pending_events == {}
        assert handler.first_event_time == {}
        assert timers.live() == []
        assert not any(
            message.startswith(logs.CHANGE_DEBOUNCING.split("{", 1)[0])
            for message in info_messages
        )

    def test_a_read_during_a_pending_write_keeps_the_write(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        _feed(handler, source, (*INOTIFY_OVERWRITE, *INOTIFY_READ))
        timers.fire_all()

        mock_updater.reingest.assert_called_once_with((source,))


class TestWithoutDebounce:
    def test_debounce_zero_still_processes_each_content_event(
        self,
        mock_updater: MagicMock,
        timers: Timers,
        source: Path,
    ) -> None:
        # Unchanged: every event goes straight through, so the write's created
        # and modified each re-ingest and the open/close pair adds nothing.
        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0, timer_factory=timers.factory
        )

        _feed(handler, source, INOTIFY_NEW_FILE_WRITE)

        assert [c.args for c in mock_updater.reingest.call_args_list] == [
            ((source,),),
            ((source,),),
        ]
        assert timers.scheduled == []
        assert handler.pending_events == {}

    def test_debounce_zero_ignores_a_read(
        self,
        mock_updater: MagicMock,
        source: Path,
    ) -> None:
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)

        _feed(handler, source, INOTIFY_READ)

        mock_updater.reingest.assert_not_called()
