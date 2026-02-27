from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable
from unittest.mock import MagicMock

import pytest
from watchdog.events import (
    DirCreatedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
)

from realtime_updater import CodeChangeEventHandler


@runtime_checkable
class _AnyProtocol(Protocol):
    pass


@pytest.fixture(autouse=True)
def _bypass_protocol_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("realtime_updater.QueryProtocol", _AnyProtocol)


@pytest.fixture
def event_handler(mock_updater: MagicMock) -> CodeChangeEventHandler:
    handler = CodeChangeEventHandler(mock_updater)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}
    return handler


def test_file_creation_flow(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Test that creating a new file triggers parsing and ingestion."""
    test_file = temp_repo / "new_file.py"
    test_file.write_text(encoding="utf-8", data="def new_func(): pass")
    event = FileCreatedEvent(str(test_file))

    event_handler.dispatch(event)

    assert mock_updater.ingestor.execute_write.call_count == 2
    mock_updater.factory.definition_processor.process_file.assert_called_once_with(
        test_file,
        "python",
        mock_updater.queries,
        mock_updater.factory.structure_processor.structural_elements,
    )
    mock_updater.ingestor.flush_all.assert_called_once()


def test_file_modification_flow(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Test that modifying a file triggers removal and re-ingestion."""
    test_file = temp_repo / "existing_file.py"
    test_file.touch()
    event = FileModifiedEvent(str(test_file))

    event_handler.dispatch(event)

    assert mock_updater.ingestor.execute_write.call_count == 2
    mock_updater.factory.definition_processor.process_file.assert_called_once_with(
        test_file,
        "python",
        mock_updater.queries,
        mock_updater.factory.structure_processor.structural_elements,
    )
    mock_updater.ingestor.flush_all.assert_called_once()


def test_file_deletion_flow(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Test that deleting a file triggers its removal from the graph."""
    test_file = temp_repo / "deleted_file.py"
    event = FileDeletedEvent(str(test_file))

    event_handler.dispatch(event)

    assert mock_updater.ingestor.execute_write.call_count == 2
    mock_updater.factory.definition_processor.process_file.assert_not_called()
    mock_updater.ingestor.flush_all.assert_called_once()


def test_irrelevant_files_are_ignored(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Test that files in ignored directories are skipped."""
    ignored_dir = temp_repo / ".git"
    ignored_dir.mkdir()
    ignored_file = ignored_dir / "config"
    ignored_file.touch()
    event = FileCreatedEvent(str(ignored_file))

    event_handler.dispatch(event)

    mock_updater.ingestor.execute_write.assert_not_called()
    mock_updater.factory.definition_processor.process_file.assert_not_called()
    mock_updater.ingestor.flush_all.assert_not_called()


def test_directory_creation_is_ignored(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Test that creating a directory does not trigger any graph operations."""
    test_dir = temp_repo / "new_dir"
    event = DirCreatedEvent(str(test_dir))

    event_handler.dispatch(event)

    mock_updater.ingestor.execute_write.assert_not_called()
    mock_updater.factory.definition_processor.process_file.assert_not_called()
    mock_updater.ingestor.flush_all.assert_not_called()


def test_unsupported_file_types_are_ignored(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Test that changing an unsupported file type is ignored after deletion query."""
    unsupported_file = temp_repo / "document.md"
    unsupported_file.write_text(encoding="utf-8", data="# Markdown file")
    event = FileModifiedEvent(str(unsupported_file))

    event_handler.dispatch(event)

    assert mock_updater.ingestor.execute_write.call_count == 2
    mock_updater.factory.definition_processor.process_file.assert_not_called()
    mock_updater.ingestor.flush_all.assert_called_once()
