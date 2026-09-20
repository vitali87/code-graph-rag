from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import (
    DirCreatedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
)

from realtime_updater import CodeChangeEventHandler


@pytest.fixture
def event_handler(mock_updater: MagicMock) -> CodeChangeEventHandler:
    handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}
    return handler


# The watcher owns event filtering and debouncing only; everything a change
# does to the graph (delete, re-parse, scoped call resolution, restore) is
# GraphUpdater.reingest, shared with the MCP tool (issue #1524). These tests
# pin the hand-off, and test_reingest.py pins what reingest itself does.


def test_file_creation_flow(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Creating a file re-ingests exactly that file."""
    test_file = temp_repo / "new_file.py"
    test_file.write_text(encoding="utf-8", data="def new_func(): pass")

    event_handler.dispatch(FileCreatedEvent(str(test_file)))

    mock_updater.reingest.assert_called_once_with((test_file,))


def test_file_modification_flow(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Modifying a file re-ingests exactly that file."""
    test_file = temp_repo / "existing_file.py"
    test_file.touch()

    event_handler.dispatch(FileModifiedEvent(str(test_file)))

    mock_updater.reingest.assert_called_once_with((test_file,))


def test_file_deletion_flow(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Deleting a file removes it through the deleted channel.

    A DELETE event names the removal explicitly rather than relying on the
    file being absent: an atomic save can recreate the path before the
    debounced handler runs, and the graph must still drop the old subtree.
    """
    test_file = temp_repo / "deleted_file.py"

    event_handler.dispatch(FileDeletedEvent(str(test_file)))

    mock_updater.reingest.assert_called_once_with((), deleted=(test_file,))


def test_irrelevant_files_are_ignored(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Files in ignored directories never reach the updater."""
    ignored_dir = temp_repo / ".git"
    ignored_dir.mkdir()
    ignored_file = ignored_dir / "config"
    ignored_file.touch()

    event_handler.dispatch(FileCreatedEvent(str(ignored_file)))

    mock_updater.reingest.assert_not_called()


def test_directory_creation_is_ignored(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Creating a directory triggers no graph operation."""
    event_handler.dispatch(DirCreatedEvent(str(temp_repo / "new_dir")))

    mock_updater.reingest.assert_not_called()


def test_non_code_files_are_reingested_too(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """A non-code file (e.g. Markdown) still goes through reingest.

    reingest routes it to the secondary tiers and creates its File node;
    the watcher does not decide per extension.
    """
    non_code_file = temp_repo / "document.md"
    non_code_file.write_text(encoding="utf-8", data="# Markdown file")

    event_handler.dispatch(FileModifiedEvent(str(non_code_file)))

    mock_updater.reingest.assert_called_once_with((non_code_file,))


def test_read_only_events_are_ignored(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """An 'opened' or 'closed_no_write' event changes nothing."""
    test_file = temp_repo / "read.py"
    test_file.touch()
    event = FileModifiedEvent(str(test_file))
    event.event_type = "opened"  # type: ignore[misc]

    event_handler._process_change(event)

    mock_updater.reingest.assert_not_called()


def test_a_refused_path_is_logged_and_later_events_still_run(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """reingest refuses a path outside the repo with ValueError; the callback
    must log it and keep serving events rather than die on the timer thread."""
    outside = temp_repo / "escape.py"
    outside.touch()
    inside = temp_repo / "kept.py"
    inside.touch()
    mock_updater.reingest.side_effect = [
        ValueError("Path is outside the repository: escape.py"),
        None,
    ]

    event_handler.dispatch(FileModifiedEvent(str(outside)))
    event_handler.dispatch(FileModifiedEvent(str(inside)))

    assert mock_updater.reingest.call_count == 2
    mock_updater.reingest.assert_called_with((inside,))
