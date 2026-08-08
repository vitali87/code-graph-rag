# Tests for realtime_updater debouncing: the hybrid strategy that prevents
# redundant graph updates during rapid file saves.

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

from codebase_rag.constants import DEFAULT_DEBOUNCE_SECONDS, DEFAULT_MAX_WAIT_SECONDS
from codebase_rag.services import QueryProtocol

# The debounce flush runs on a daemon timer thread. A fixed sleep sized to the
# debounce window races that thread's scheduling on a loaded runner, which is
# what made these tests fail on unrelated PRs and pass on re-run (issue #1005).
# Waiting on the CONDITION instead makes each assertion depend on ordering, and
# a generous deadline costs nothing when the thread is prompt.
WAIT_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.01


def _wait_until(predicate: Any, timeout: float = WAIT_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_SECONDS)
    return predicate()


def _wait_for_flushes(
    mock_ingestor: MockQueryIngestor,
    count: int = 1,
    timeout: float = WAIT_TIMEOUT_SECONDS,
) -> int:
    _wait_until(lambda: mock_ingestor.flush_all.call_count >= count, timeout)
    return int(mock_ingestor.flush_all.call_count)


def _wait_for_quiescence(handler: Any, timeout: float = WAIT_TIMEOUT_SECONDS) -> bool:
    """Wait until no debounce timer and no pending event remain.

    Reading `flush_all.call_count` before this holds samples a batch that is
    still forming: the max-wait flush can land while the final event's timer
    is still scheduled, so the count reflects an intermediate state.
    """
    return _wait_until(
        lambda: not handler.timers and not handler.pending_events, timeout
    )


class ManualTimer:
    """A `threading.Timer` stand-in that only fires when the test says so.

    Removes the wall clock from the batching assertions entirely: no elapsed
    time can flush a batch mid-dispatch, so the assertion depends on ordering
    alone rather than on the runner keeping up.
    """

    pending: ClassVar[list[ManualTimer]] = []

    def __init__(
        self, interval: float, function: Any, args: Any = None, kwargs: Any = None
    ) -> None:
        self.interval = interval
        self.function = function
        self.args = args or []
        self.kwargs = kwargs or {}
        self.daemon = True
        self.cancelled = False

    def start(self) -> None:
        ManualTimer.pending.append(self)

    def cancel(self) -> None:
        self.cancelled = True
        if self in ManualTimer.pending:
            ManualTimer.pending.remove(self)

    @classmethod
    def fire_all(cls) -> int:
        """Run every scheduled callback, newest schedule per path last."""
        fired = 0
        while cls.pending:
            timer = cls.pending.pop(0)
            if timer.cancelled:
                continue
            timer.function(*timer.args, **timer.kwargs)
            fired += 1
        return fired

    @classmethod
    def reset(cls) -> None:
        cls.pending = []


class MockQueryIngestor:
    def __init__(self) -> None:
        self.execute_write = MagicMock()
        self.flush_all = MagicMock()
        self.fetch_all = MagicMock(return_value=[])
        self.ensure_node_batch = MagicMock()
        self.ensure_relationship_batch = MagicMock()

    def __enter__(self) -> MockQueryIngestor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


# Register MockQueryIngestor as implementing QueryProtocol for isinstance checks
QueryProtocol.register(MockQueryIngestor)


class TestCodeChangeEventHandlerDebounce:
    @pytest.fixture(autouse=True)
    def _patch_ignore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from codebase_rag import constants as cs

        patched = cs.IGNORE_PATTERNS - {"tmp"}
        monkeypatch.setattr(cs, "IGNORE_PATTERNS", patched)
        monkeypatch.setattr("realtime_updater.IGNORE_PATTERNS", patched)

    @pytest.fixture
    def mock_ingestor(self) -> MockQueryIngestor:
        return MockQueryIngestor()

    @pytest.fixture
    def mock_updater(
        self, tmp_path: Path, mock_ingestor: MockQueryIngestor
    ) -> MagicMock:
        updater = MagicMock()
        updater.repo_path = tmp_path
        updater.ingestor = mock_ingestor
        updater.remove_file_from_state = MagicMock()
        updater.factory = MagicMock()
        updater.factory.definition_processor.process_file = MagicMock(return_value=None)
        updater._process_function_calls = MagicMock()
        updater.parsers = {}
        updater.queries = {}
        updater.ast_cache = {}
        return updater

    @pytest.fixture
    def sample_file(self, tmp_path: Path) -> Path:
        test_file = tmp_path / "test.py"
        test_file.write_text("# test file")
        return test_file

    def test_handler_initialization_with_debounce(
        self, mock_updater: MagicMock
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=5, max_wait_seconds=30
        )

        assert handler.debounce_seconds == 5
        assert handler.max_wait_seconds == 30
        assert handler.debounce_enabled is True
        assert len(handler.timers) == 0
        assert len(handler.first_event_time) == 0
        assert len(handler.pending_events) == 0

    def test_handler_initialization_without_debounce(
        self, mock_updater: MagicMock
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0, max_wait_seconds=30
        )

        assert handler.debounce_seconds == 0
        assert handler.debounce_enabled is False

    def test_handler_uses_default_constants(self, mock_updater: MagicMock) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(mock_updater)

        assert handler.debounce_seconds == DEFAULT_DEBOUNCE_SECONDS
        assert handler.max_wait_seconds == DEFAULT_MAX_WAIT_SECONDS

    def test_is_relevant_filters_ignored_patterns(
        self, mock_updater: MagicMock, tmp_path: Path
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(mock_updater)

        assert handler._is_relevant(str(tmp_path / ".git" / "config")) is False
        assert handler._is_relevant(str(tmp_path / "node_modules" / "pkg.js")) is False
        assert handler._is_relevant(str(tmp_path / "__pycache__" / "mod.pyc")) is False

        assert handler._is_relevant(str(tmp_path / "main.py")) is True
        assert handler._is_relevant(str(tmp_path / "src" / "lib.rs")) is True
        assert handler._is_relevant(str(tmp_path / "app.js")) is True

    def test_dispatch_ignores_directories(
        self, mock_updater: MagicMock, mock_ingestor: MockQueryIngestor, tmp_path: Path
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.1, max_wait_seconds=1
        )

        event = FileModifiedEvent(str(tmp_path / "some_dir"))
        # watchdog derives is_directory from the event type; force it True here.
        object.__setattr__(event, "is_directory", True)

        handler.dispatch(event)

        assert len(handler.timers) == 0
        mock_ingestor.execute_write.assert_not_called()

    def test_debounce_batches_rapid_events(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        # Driven by a manual timer: no elapsed time can flush the batch during
        # the dispatch loop, so the assertion depends on ordering alone.
        ManualTimer.reset()
        handler = CodeChangeEventHandler(
            mock_updater,
            debounce_seconds=1.0,
            max_wait_seconds=30,
            timer_factory=ManualTimer,
        )

        for _ in range(5):
            handler.dispatch(FileModifiedEvent(str(sample_file)))

        # Five rapid saves coalesced into one pending entry, and nothing has
        # been flushed yet because the window has not been allowed to expire.
        assert len(handler.pending_events) == 1
        assert mock_ingestor.flush_all.call_count == 0

        ManualTimer.fire_all()

        assert mock_ingestor.flush_all.call_count == 1
        assert handler.pending_events == {}

    def test_no_debounce_processes_immediately(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0, max_wait_seconds=30
        )

        event = FileModifiedEvent(str(sample_file))
        handler.dispatch(event)

        assert len(handler.pending_events) == 0
        assert len(handler.timers) == 0
        mock_ingestor.flush_all.assert_called_once()

    def test_max_wait_forces_update(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.5, max_wait_seconds=0.3
        )

        event = FileModifiedEvent(str(sample_file))
        handler.dispatch(event)

        time.sleep(0.4)

        event2 = FileModifiedEvent(str(sample_file))
        handler.dispatch(event2)

        assert _wait_for_flushes(mock_ingestor) >= 1

    def test_different_files_tracked_separately(
        self, mock_updater: MagicMock, tmp_path: Path
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file1.write_text("# file 1")
        file2.write_text("# file 2")

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.2, max_wait_seconds=5
        )

        event1 = FileModifiedEvent(str(file1))
        event2 = FileModifiedEvent(str(file2))

        handler.dispatch(event1)
        handler.dispatch(event2)

        assert len(handler.pending_events) == 2
        assert len(handler.timers) == 2

    def test_timer_cleanup_after_processing(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.1, max_wait_seconds=5
        )

        event = FileModifiedEvent(str(sample_file))
        handler.dispatch(event)

        assert len(handler.pending_events) == 1
        assert len(handler.first_event_time) == 1

        assert _wait_until(lambda: not handler.timers)

        assert len(handler.pending_events) == 0
        assert len(handler.first_event_time) == 0
        assert len(handler.timers) == 0

    def test_created_event_triggers_debounce(
        self, mock_updater: MagicMock, tmp_path: Path
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        new_file = tmp_path / "new_file.py"
        new_file.write_text("# new file")

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.2, max_wait_seconds=5
        )

        event = FileCreatedEvent(str(new_file))
        handler.dispatch(event)

        assert len(handler.pending_events) == 1

    def test_deleted_event_triggers_debounce(
        self, mock_updater: MagicMock, sample_file: Path
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.2, max_wait_seconds=5
        )

        event = FileDeletedEvent(str(sample_file))
        handler.dispatch(event)

        assert len(handler.pending_events) == 1

    def test_thread_safety_concurrent_events(
        self, mock_updater: MagicMock, tmp_path: Path
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=5.0, max_wait_seconds=30
        )

        files = [tmp_path / f"file{i}.py" for i in range(10)]
        for f in files:
            f.write_text(f"# {f.name}")

        def send_events(file_path: Path) -> None:
            for _ in range(5):
                event = FileModifiedEvent(str(file_path))
                handler.dispatch(event)
                time.sleep(0.02)

        threads = [threading.Thread(target=send_events, args=(f,)) for f in files[:5]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(handler.pending_events) == 5


class TestTimerFactoryContract:
    """`start()` runs under the handler's lock, which the callback re-acquires.

    A factory that fires during `start()` therefore deadlocks the handler, so
    the contract is that it queues the callback instead.
    """

    @pytest.fixture(autouse=True)
    def _patch_ignore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from codebase_rag import constants as cs

        patched = cs.IGNORE_PATTERNS - {"tmp"}
        monkeypatch.setattr(cs, "IGNORE_PATTERNS", patched)
        monkeypatch.setattr("realtime_updater.IGNORE_PATTERNS", patched)

    @pytest.fixture
    def mock_ingestor(self) -> MockQueryIngestor:
        return MockQueryIngestor()

    @pytest.fixture
    def mock_updater(
        self, tmp_path: Path, mock_ingestor: MockQueryIngestor
    ) -> MagicMock:
        updater = MagicMock()
        updater.repo_path = tmp_path
        updater.ingestor = mock_ingestor
        updater.remove_file_from_state = MagicMock()
        updater.factory = MagicMock()
        updater.factory.definition_processor.process_file = MagicMock(return_value=None)
        updater._process_function_calls = MagicMock()
        updater.parsers = {}
        updater.queries = {}
        updater.ast_cache = {}
        return updater

    @pytest.fixture
    def sample_file(self, tmp_path: Path) -> Path:
        test_file = tmp_path / "test.py"
        test_file.write_text("# test file")
        return test_file

    def test_manual_timer_does_not_fire_during_start(self) -> None:
        ManualTimer.reset()
        fired: list[str] = []
        timer = ManualTimer(0.0, lambda: fired.append("fired"))

        timer.start()

        assert fired == []
        assert ManualTimer.pending == [timer]

    def test_dispatching_under_a_manual_timer_flushes_nothing(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        # The real deadlock check: dispatch takes the lock and calls start(),
        # and _process_debounced_change re-acquires that same lock.
        from realtime_updater import CodeChangeEventHandler

        ManualTimer.reset()
        handler = CodeChangeEventHandler(
            mock_updater,
            debounce_seconds=1.0,
            max_wait_seconds=30,
            timer_factory=ManualTimer,
        )

        handler.dispatch(FileModifiedEvent(str(sample_file)))

        assert mock_ingestor.flush_all.call_count == 0
        assert handler.timers

    def test_the_factory_result_supports_cancel(
        self,
        mock_updater: MagicMock,
        sample_file: Path,
    ) -> None:
        # A newer event for the same path supersedes the scheduled timer.
        from realtime_updater import CodeChangeEventHandler

        ManualTimer.reset()
        handler = CodeChangeEventHandler(
            mock_updater,
            debounce_seconds=1.0,
            max_wait_seconds=30,
            timer_factory=ManualTimer,
        )

        handler.dispatch(FileModifiedEvent(str(sample_file)))
        first = handler.timers[next(iter(handler.timers))]
        handler.dispatch(FileModifiedEvent(str(sample_file)))

        assert first.cancelled
        assert first.daemon is True


class TestDebounceValidation:
    def test_validate_non_negative_float_accepts_zero(self) -> None:
        from realtime_updater import _validate_non_negative_float

        assert _validate_non_negative_float(0) == 0
        assert _validate_non_negative_float(0.0) == 0.0

    def test_validate_non_negative_float_accepts_positive(self) -> None:
        from realtime_updater import _validate_non_negative_float

        assert _validate_non_negative_float(5) == 5
        assert _validate_non_negative_float(0.5) == 0.5
        assert _validate_non_negative_float(100) == 100

    def test_validate_non_negative_float_rejects_negative(self) -> None:
        import typer

        from realtime_updater import _validate_non_negative_float

        with pytest.raises(typer.BadParameter):
            _validate_non_negative_float(-1)

        with pytest.raises(typer.BadParameter):
            _validate_non_negative_float(-0.1)


class TestDebounceIntegration:
    @pytest.fixture(autouse=True)
    def _patch_ignore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from codebase_rag import constants as cs

        patched = cs.IGNORE_PATTERNS - {"tmp"}
        monkeypatch.setattr(cs, "IGNORE_PATTERNS", patched)
        monkeypatch.setattr("realtime_updater.IGNORE_PATTERNS", patched)

    @pytest.fixture
    def mock_ingestor(self) -> MockQueryIngestor:
        return MockQueryIngestor()

    @pytest.fixture
    def mock_updater(
        self, tmp_path: Path, mock_ingestor: MockQueryIngestor
    ) -> MagicMock:
        updater = MagicMock()
        updater.repo_path = tmp_path
        updater.ingestor = mock_ingestor
        updater.remove_file_from_state = MagicMock()
        updater.factory = MagicMock()
        updater.factory.definition_processor.process_file = MagicMock(return_value=None)
        updater._process_function_calls = MagicMock()
        updater.parsers = {}
        updater.queries = {}
        updater.ast_cache = {}
        return updater

    def test_realistic_rapid_save_scenario(
        self, mock_updater: MagicMock, mock_ingestor: MockQueryIngestor, tmp_path: Path
    ) -> None:
        """
        Simulate realistic rapid save scenario:
        - User saves file 10 times over 3 seconds
        - With 0.5s debounce and 2s max_wait, should result in ~2-4 updates
        """
        from realtime_updater import CodeChangeEventHandler

        test_file = tmp_path / "editor.py"
        test_file.write_text("# editing")

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.5, max_wait_seconds=2
        )

        for i in range(10):
            event = FileModifiedEvent(str(test_file))
            handler.dispatch(event)
            time.sleep(0.3)

        # Sampling after the first flush would read a batch still forming: the
        # max-wait flush can land while the final event's timer is pending.
        assert _wait_for_quiescence(handler), "debounce never settled"
        call_count = mock_ingestor.flush_all.call_count

        # The claim is that saves BATCH: max_wait forces at least one update,
        # and ten saves must not produce ten of them. An exact upper bound
        # would just be measuring the runner, which is what made this file
        # flaky in the first place.
        assert 1 <= call_count < 10, f"Expected batching, got {call_count} updates"

    def test_single_edit_after_quiet_period(
        self, mock_updater: MagicMock, mock_ingestor: MockQueryIngestor, tmp_path: Path
    ) -> None:
        from realtime_updater import CodeChangeEventHandler

        test_file = tmp_path / "single.py"
        test_file.write_text("# single edit")

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.1, max_wait_seconds=5
        )

        event = FileModifiedEvent(str(test_file))
        handler.dispatch(event)

        assert _wait_for_flushes(mock_ingestor) == 1
        assert _wait_for_quiescence(handler), "debounce never settled"
        assert mock_ingestor.flush_all.call_count == 1
