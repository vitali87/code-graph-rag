import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from realtime_updater import CodeChangeEventHandler
from codebase_rag.graph_updater import GraphUpdater
from watchdog.events import (
    DirCreatedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
)


class TestRealtimeUpdater(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.temp_dir)

        # Mock the GraphUpdater and its dependencies
        self.mock_updater = MagicMock(spec=GraphUpdater)
        self.mock_updater.repo_path = self.repo_path
        self.mock_updater.ingestor = MagicMock()
        self.mock_updater.parsers = {"python": MagicMock()}

        self.event_handler = CodeChangeEventHandler(self.mock_updater)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir)

    def test_file_creation_flow(self) -> None:
        """Test that creating a new file triggers parsing and ingestion."""
        test_file = self.repo_path / "new_file.py"
        test_file.write_text("def new_func(): pass")
        event = FileCreatedEvent(str(test_file))
        self.event_handler.dispatch(event)

        self.mock_updater.ingestor.execute_write.assert_called_once()
        self.mock_updater.parse_and_ingest_file.assert_called_once()
        self.mock_updater.ingestor.flush_all.assert_called_once()

    def test_file_modification_flow(self) -> None:
        """Test that modifying a file triggers removal and re-ingestion."""
        test_file = self.repo_path / "existing_file.py"
        test_file.touch()
        event = FileModifiedEvent(str(test_file))
        self.event_handler.dispatch(event)

        self.mock_updater.ingestor.execute_write.assert_called_once()
        self.mock_updater.parse_and_ingest_file.assert_called_once()
        self.mock_updater.ingestor.flush_all.assert_called_once()

    def test_file_deletion_flow(self) -> None:
        """Test that deleting a file triggers its removal from the graph."""
        test_file = self.repo_path / "deleted_file.py"
        event = FileDeletedEvent(str(test_file))
        self.event_handler.dispatch(event)

        self.mock_updater.ingestor.execute_write.assert_called_once()
        self.mock_updater.parse_and_ingest_file.assert_not_called()
        self.mock_updater.ingestor.flush_all.assert_called_once()

    def test_irrelevant_files_are_ignored(self) -> None:
        """Test that files in ignored directories are skipped."""
        ignored_file = self.repo_path / ".git" / "config"
        os.makedirs(ignored_file.parent)
        ignored_file.touch()
        event = FileCreatedEvent(str(ignored_file))
        self.event_handler.dispatch(event)

        self.mock_updater.ingestor.execute_write.assert_not_called()
        self.mock_updater.parse_and_ingest_file.assert_not_called()
        self.mock_updater.ingestor.flush_all.assert_not_called()

    def test_directory_creation_is_ignored(self) -> None:
        """Test that creating a directory does not trigger any graph operations."""
        test_dir = self.repo_path / "new_dir"
        event = DirCreatedEvent(str(test_dir))
        self.event_handler.dispatch(event)

        self.mock_updater.ingestor.execute_write.assert_not_called()
        self.mock_updater.parse_and_ingest_file.assert_not_called()
        self.mock_updater.ingestor.flush_all.assert_not_called()

    def test_unsupported_file_types_are_ignored(self) -> None:
        """Test that changing an unsupported file type is ignored after deletion query."""
        unsupported_file = self.repo_path / "document.md"
        unsupported_file.write_text("# Markdown file")
        event = FileModifiedEvent(str(unsupported_file))
        self.event_handler.dispatch(event)

        # The deletion query runs first, which is a safe default.
        self.mock_updater.ingestor.execute_write.assert_called_once()
        # Crucially, we assert that no parsing is attempted.
        self.mock_updater.parse_and_ingest_file.assert_not_called()
        # The flush should still be called.
        self.mock_updater.ingestor.flush_all.assert_called_once()


if __name__ == "__main__":
    unittest.main()
