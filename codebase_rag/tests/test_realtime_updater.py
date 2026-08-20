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
    handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
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

    # 3 execute_write calls: DELETE_MODULE, DELETE_FILE, DELETE_CALLS
    assert mock_updater.ingestor.execute_write.call_count == 3
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

    # 3 execute_write calls: DELETE_MODULE, DELETE_FILE, DELETE_CALLS
    assert mock_updater.ingestor.execute_write.call_count == 3
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

    # 3 execute_write calls: DELETE_MODULE, DELETE_FILE, DELETE_CALLS
    assert mock_updater.ingestor.execute_write.call_count == 3
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


def test_non_code_files_create_file_nodes(
    event_handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    """Test that non-code files (like .md) create File nodes but skip AST parsing."""
    non_code_file = temp_repo / "document.md"
    non_code_file.write_text(encoding="utf-8", data="# Markdown file")
    event = FileModifiedEvent(str(non_code_file))

    event_handler.dispatch(event)

    # 3 execute_write calls: DELETE_MODULE, DELETE_FILE, DELETE_CALLS
    assert mock_updater.ingestor.execute_write.call_count == 3
    # AST parsing is skipped for non-code files
    mock_updater.factory.definition_processor.process_file.assert_not_called()
    # But File node creation IS called for all file types
    mock_updater.factory.structure_processor.process_generic_file.assert_called_once_with(
        non_code_file, "document.md"
    )
    mock_updater.ingestor.flush_all.assert_called_once()


class TestSemanticFrontendReruns:
    # Issue #1229 phase 3: semantic facts are location-keyed against the
    # compiler's view of the whole module, so a change in one file can rebind
    # calls in unchanged files; the applicable frontend must re-run on the
    # watch path before the CALLS recompute.

    def _fire(self, handler: CodeChangeEventHandler, path: Path) -> None:
        path.write_text("// change\n", encoding="utf-8")
        handler._process_change(FileModifiedEvent(str(path)))

    def test_go_change_reruns_the_go_frontend(
        self, event_handler: CodeChangeEventHandler, mock_updater: MagicMock
    ) -> None:
        self._fire(event_handler, mock_updater.repo_path / "svc.go")
        mock_updater._run_go_frontend.assert_called_once()
        mock_updater._run_csharp_frontend.assert_not_called()
        mock_updater._rehydrate_go_type_locations.assert_called_once()
        mock_updater._rehydrate_function_locations.assert_called_once()
        mock_updater._join_go_implements.assert_called_once()

    def test_csharp_change_reruns_the_roslyn_frontend(
        self, event_handler: CodeChangeEventHandler, mock_updater: MagicMock
    ) -> None:
        self._fire(event_handler, mock_updater.repo_path / "Svc.cs")
        mock_updater._run_csharp_frontend.assert_called_once()
        mock_updater._run_go_frontend.assert_not_called()
        mock_updater._rehydrate_csharp_type_locations.assert_called_once()
        mock_updater._join_csharp_partials.assert_called_once()

    def test_java_change_reruns_the_javac_frontend(
        self, event_handler: CodeChangeEventHandler, mock_updater: MagicMock
    ) -> None:
        # javac facts are keyed by (file, line, byte col): an edit that shifts
        # a call would otherwise keep binding through the previous run's
        # positions, and a stale external proof would keep suppressing an edge.
        self._fire(event_handler, mock_updater.repo_path / "Svc.java")
        mock_updater._run_java_frontend.assert_called_once()
        mock_updater._rehydrate_function_locations.assert_called_once()
        mock_updater._run_go_frontend.assert_not_called()

    def test_python_change_touches_no_semantic_frontend(
        self, event_handler: CodeChangeEventHandler, mock_updater: MagicMock
    ) -> None:
        self._fire(event_handler, mock_updater.repo_path / "app.py")
        mock_updater._run_go_frontend.assert_not_called()
        mock_updater._run_csharp_frontend.assert_not_called()
        mock_updater._run_java_frontend.assert_not_called()

    def test_go_deletion_also_reruns_the_frontend(
        self, event_handler: CodeChangeEventHandler, mock_updater: MagicMock
    ) -> None:
        # Removing a file changes the module's bindings just as an edit does.
        gone = mock_updater.repo_path / "gone.go"
        gone.write_text("package x\n", encoding="utf-8")
        gone.unlink()
        event_handler._process_change(FileDeletedEvent(str(gone)))
        mock_updater._run_go_frontend.assert_called_once()
        mock_updater._join_go_implements.assert_called_once()
