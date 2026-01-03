"""
Tests for the realtime_updater debouncing functionality.

These tests verify the hybrid debounce strategy that prevents redundant
graph updates during rapid file saves.
"""

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

from codebase_rag.constants import DEFAULT_DEBOUNCE_SECONDS, DEFAULT_MAX_WAIT_SECONDS
from codebase_rag.services import QueryProtocol


class MockQueryIngestor:
    """Mock ingestor that satisfies both IngestorProtocol and QueryProtocol."""

    def __init__(self) -> None:
        self.execute_write = MagicMock()
        self.flush_all = MagicMock()
        self.fetch_all = MagicMock(return_value=[])
        self.ensure_node_batch = MagicMock()
        self.ensure_relationship_batch = MagicMock()

    def __enter__(self) -> "MockQueryIngestor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


# Register MockQueryIngestor as implementing QueryProtocol for isinstance checks
QueryProtocol.register(MockQueryIngestor)


class TestCodeChangeEventHandlerDebounce:
    """Tests for the CodeChangeEventHandler debouncing logic."""

    @pytest.fixture
    def mock_ingestor(self) -> MockQueryIngestor:
        """Create a mock ingestor that satisfies QueryProtocol."""
        return MockQueryIngestor()

    @pytest.fixture
    def mock_updater(
        self, tmp_path: Path, mock_ingestor: MockQueryIngestor
    ) -> MagicMock:
        """Create a mock GraphUpdater with required attributes."""
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
        """Create a sample file for testing."""
        test_file = tmp_path / "test.py"
        test_file.write_text("# test file")
        return test_file

    def test_handler_initialization_with_debounce(
        self, mock_updater: MagicMock
    ) -> None:
        """Test that handler initializes with correct debounce settings."""
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
        """Test that handler initializes correctly when debouncing is disabled."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0, max_wait_seconds=30
        )

        assert handler.debounce_seconds == 0
        assert handler.debounce_enabled is False

    def test_handler_uses_default_constants(self, mock_updater: MagicMock) -> None:
        """Test that handler uses default constants when not specified."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(mock_updater)

        assert handler.debounce_seconds == DEFAULT_DEBOUNCE_SECONDS
        assert handler.max_wait_seconds == DEFAULT_MAX_WAIT_SECONDS

    def test_is_relevant_filters_ignored_patterns(
        self, mock_updater: MagicMock, tmp_path: Path
    ) -> None:
        """Test that _is_relevant correctly filters out ignored paths."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(mock_updater)

        # Should be ignored (directories in ignore patterns)
        assert handler._is_relevant(str(tmp_path / ".git" / "config")) is False
        assert handler._is_relevant(str(tmp_path / "node_modules" / "pkg.js")) is False
        assert handler._is_relevant(str(tmp_path / "__pycache__" / "mod.pyc")) is False

        # Should be relevant
        assert handler._is_relevant(str(tmp_path / "main.py")) is True
        assert handler._is_relevant(str(tmp_path / "src" / "lib.rs")) is True
        assert handler._is_relevant(str(tmp_path / "app.js")) is True

    def test_dispatch_ignores_directories(
        self, mock_updater: MagicMock, mock_ingestor: MockQueryIngestor, tmp_path: Path
    ) -> None:
        """Test that dispatch ignores directory events."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.1, max_wait_seconds=1
        )

        # Create event that is marked as directory
        event = FileModifiedEvent(str(tmp_path / "some_dir"))
        # The is_directory property is set by watchdog based on the event type
        # For FileModifiedEvent, we need to check is_directory attribute
        object.__setattr__(event, "is_directory", True)

        handler.dispatch(event)

        # No timer should be created for directory events
        assert len(handler.timers) == 0
        mock_ingestor.execute_write.assert_not_called()

    def test_debounce_batches_rapid_events(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        """Test that rapid events are batched into a single update."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.2, max_wait_seconds=5
        )

        # Simulate 5 rapid saves
        for _ in range(5):
            event = FileModifiedEvent(str(sample_file))
            handler.dispatch(event)
            time.sleep(0.05)  # 50ms between saves

        # Should have one pending event
        assert len(handler.pending_events) == 1

        # Wait for debounce to complete
        time.sleep(0.4)

        # After debounce, ingestor should have been called only once
        mock_ingestor.flush_all.assert_called_once()

    def test_no_debounce_processes_immediately(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        """Test that events are processed immediately when debounce is disabled."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0, max_wait_seconds=30
        )

        event = FileModifiedEvent(str(sample_file))
        handler.dispatch(event)

        # Should process immediately (no pending events)
        assert len(handler.pending_events) == 0
        assert len(handler.timers) == 0
        mock_ingestor.flush_all.assert_called_once()

    def test_max_wait_forces_update(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        """Test that max_wait forces an update even during continuous editing."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.5, max_wait_seconds=0.3
        )

        # First event
        event = FileModifiedEvent(str(sample_file))
        handler.dispatch(event)

        # Wait until max_wait is exceeded
        time.sleep(0.4)

        # Second event should trigger immediate processing due to max_wait
        event2 = FileModifiedEvent(str(sample_file))
        handler.dispatch(event2)

        # Give time for processing
        time.sleep(0.15)

        # Should have processed at least once due to max_wait
        assert mock_ingestor.flush_all.call_count >= 1

    def test_different_files_tracked_separately(
        self, mock_updater: MagicMock, tmp_path: Path
    ) -> None:
        """Test that different files are debounced independently."""
        from realtime_updater import CodeChangeEventHandler

        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file1.write_text("# file 1")
        file2.write_text("# file 2")

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.2, max_wait_seconds=5
        )

        # Events for different files
        event1 = FileModifiedEvent(str(file1))
        event2 = FileModifiedEvent(str(file2))

        handler.dispatch(event1)
        handler.dispatch(event2)

        # Should have two pending events
        assert len(handler.pending_events) == 2
        assert len(handler.timers) == 2

    def test_timer_cleanup_after_processing(
        self,
        mock_updater: MagicMock,
        mock_ingestor: MockQueryIngestor,
        sample_file: Path,
    ) -> None:
        """Test that timers and state are cleaned up after processing."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.1, max_wait_seconds=5
        )

        event = FileModifiedEvent(str(sample_file))
        handler.dispatch(event)

        # Should have pending state
        assert len(handler.pending_events) == 1
        assert len(handler.first_event_time) == 1

        # Wait for processing
        time.sleep(0.25)

        # State should be cleaned up
        assert len(handler.pending_events) == 0
        assert len(handler.first_event_time) == 0
        assert len(handler.timers) == 0

    def test_created_event_triggers_debounce(
        self, mock_updater: MagicMock, tmp_path: Path
    ) -> None:
        """Test that created events are also debounced."""
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
        """Test that deleted events are also debounced."""
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
        """Test thread safety when multiple events arrive concurrently."""
        from realtime_updater import CodeChangeEventHandler

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.3, max_wait_seconds=5
        )

        files = [tmp_path / f"file{i}.py" for i in range(10)]
        for f in files:
            f.write_text(f"# {f.name}")

        def send_events(file_path: Path) -> None:
            for _ in range(5):
                event = FileModifiedEvent(str(file_path))
                handler.dispatch(event)
                time.sleep(0.02)

        # Send events from multiple threads
        threads = [threading.Thread(target=send_events, args=(f,)) for f in files[:5]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have 5 pending events (one per file)
        assert len(handler.pending_events) == 5


class TestDebounceValidation:
    """Tests for CLI validation of debounce parameters."""

    def test_validate_non_negative_float_accepts_zero(self) -> None:
        """Test that zero is accepted as a valid debounce value."""
        from realtime_updater import _validate_non_negative_float

        assert _validate_non_negative_float(0) == 0
        assert _validate_non_negative_float(0.0) == 0.0

    def test_validate_non_negative_float_accepts_positive(self) -> None:
        """Test that positive values are accepted."""
        from realtime_updater import _validate_non_negative_float

        assert _validate_non_negative_float(5) == 5
        assert _validate_non_negative_float(0.5) == 0.5
        assert _validate_non_negative_float(100) == 100

    def test_validate_non_negative_float_rejects_negative(self) -> None:
        """Test that negative values are rejected."""
        import typer

        from realtime_updater import _validate_non_negative_float

        with pytest.raises(typer.BadParameter):
            _validate_non_negative_float(-1)

        with pytest.raises(typer.BadParameter):
            _validate_non_negative_float(-0.1)


class TestDebounceIntegration:
    """Integration tests for debounce with real timing."""

    @pytest.fixture
    def mock_ingestor(self) -> MockQueryIngestor:
        """Create a mock ingestor that satisfies QueryProtocol."""
        return MockQueryIngestor()

    @pytest.fixture
    def mock_updater(
        self, tmp_path: Path, mock_ingestor: MockQueryIngestor
    ) -> MagicMock:
        """Create a mock GraphUpdater."""
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

        # Simulate 10 saves over 3 seconds
        for i in range(10):
            event = FileModifiedEvent(str(test_file))
            handler.dispatch(event)
            time.sleep(0.3)

        # Wait for final debounce
        time.sleep(0.7)

        # Should have batched into fewer updates due to max_wait and debounce
        # With max_wait=2s and 3s total time, expect ~2-4 updates
        call_count = mock_ingestor.flush_all.call_count
        assert 1 <= call_count <= 4, f"Expected 1-4 updates, got {call_count}"

    def test_single_edit_after_quiet_period(
        self, mock_updater: MagicMock, mock_ingestor: MockQueryIngestor, tmp_path: Path
    ) -> None:
        """Test that a single edit after quiet period is processed correctly."""
        from realtime_updater import CodeChangeEventHandler

        test_file = tmp_path / "single.py"
        test_file.write_text("# single edit")

        handler = CodeChangeEventHandler(
            mock_updater, debounce_seconds=0.1, max_wait_seconds=5
        )

        event = FileModifiedEvent(str(test_file))
        handler.dispatch(event)

        # Wait for debounce
        time.sleep(0.25)

        # Should have exactly one update
        mock_ingestor.flush_all.assert_called_once()
