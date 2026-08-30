from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import (
    FileClosedNoWriteEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileOpenedEvent,
    FileSystemEvent,
)

from realtime_updater import CodeChangeEventHandler

# The watcher decides WHICH events reach the graph; GraphUpdater.reingest
# decides what happens to them (issue #1524; see test_reingest.py for the
# delete/re-parse/File-node contract these tests used to assert inline).


@pytest.fixture
def handler(mock_updater: MagicMock) -> CodeChangeEventHandler:
    h = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
    h.ignore_patterns = h.ignore_patterns - {"tmp", "temp"}
    return h


def _make_event(event_type: str, src_path: str) -> FileSystemEvent:
    ev = MagicMock(spec=FileSystemEvent)
    ev.event_type = event_type
    ev.src_path = src_path
    ev.is_directory = False
    return ev


class TestEventFiltering:
    def test_modified_event_is_processed(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "app.py"
        f.write_text("x = 1", encoding="utf-8")
        handler.dispatch(FileModifiedEvent(str(f)))
        mock_updater.reingest.assert_called_once_with((f,))

    def test_created_event_is_processed(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "new.py"
        f.write_text("y = 2", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        mock_updater.reingest.assert_called_once_with((f,))

    def test_deleted_event_is_processed(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "gone.py"
        handler.dispatch(FileDeletedEvent(str(f)))
        mock_updater.reingest.assert_called_once_with((), deleted=(f,))

    @pytest.mark.parametrize(
        "event_cls", [FileOpenedEvent, FileClosedNoWriteEvent], ids=["opened", "closed"]
    )
    def test_read_only_events_are_ignored(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        temp_repo: Path,
        event_cls: type[FileSystemEvent],
    ) -> None:
        f = temp_repo / "read_only.py"
        f.touch()
        handler.dispatch(event_cls(str(f)))
        mock_updater.reingest.assert_not_called()

    def test_access_event_is_ignored(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "accessed.py"
        f.touch()
        handler.dispatch(_make_event("access", str(f)))
        mock_updater.reingest.assert_not_called()

    def test_bytes_paths_are_decoded(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "raw.py"
        f.touch()
        handler._process_change(_make_event("modified", str(f).encode()))
        mock_updater.reingest.assert_called_once_with((f,))


class TestNonCodeFiles:
    @pytest.mark.parametrize("filename", ["readme.md", "config.json"])
    def test_non_code_files_reach_reingest(
        self,
        handler: CodeChangeEventHandler,
        mock_updater: MagicMock,
        temp_repo: Path,
        filename: str,
    ) -> None:
        # The watcher does not decide per extension: File nodes and the
        # secondary tiers are reingest's business.
        f = temp_repo / filename
        f.write_text("x", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        mock_updater.reingest.assert_called_once_with((f,))


class TestMixedEventSequences:
    def test_rapid_create_modify_delete(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        """A create/modify/delete burst ends with the file removed."""
        f = temp_repo / "ephemeral.py"
        f.write_text("a = 1", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        f.write_text("a = 2", encoding="utf-8")
        handler.dispatch(FileModifiedEvent(str(f)))
        f.unlink()
        handler.dispatch(FileDeletedEvent(str(f)))

        assert [c.args for c in mock_updater.reingest.call_args_list] == [
            ((f,),),
            ((f,),),
            ((),),
        ]
        assert mock_updater.reingest.call_args_list[-1].kwargs == {"deleted": (f,)}

    def test_multiple_files_changed(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f1 = temp_repo / "a.py"
        f2 = temp_repo / "b.py"
        f1.write_text("x = 1", encoding="utf-8")
        f2.write_text("y = 2", encoding="utf-8")

        handler.dispatch(FileModifiedEvent(str(f1)))
        handler.dispatch(FileModifiedEvent(str(f2)))

        assert [c.args for c in mock_updater.reingest.call_args_list] == [
            ((f1,),),
            ((f2,),),
        ]
