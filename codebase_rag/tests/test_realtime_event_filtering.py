from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable
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

from codebase_rag import constants as cs
from realtime_updater import CodeChangeEventHandler


@runtime_checkable
class _AnyProtocol(Protocol):
    pass


@pytest.fixture(autouse=True)
def _bypass_protocol_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("realtime_updater.QueryProtocol", _AnyProtocol)


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
        assert mock_updater.ingestor.execute_write.call_count == 3

    def test_created_event_is_processed(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "new.py"
        f.write_text("y = 2", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        assert mock_updater.ingestor.execute_write.call_count == 3
        mock_updater.ingestor.flush_all.assert_called_once()

    def test_deleted_event_is_processed(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "gone.py"
        handler.dispatch(FileDeletedEvent(str(f)))
        assert mock_updater.ingestor.execute_write.call_count == 3
        mock_updater.factory.definition_processor.process_file.assert_not_called()
        mock_updater.factory.structure_processor.process_generic_file.assert_not_called()

    def test_opened_event_is_ignored(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "read_only.py"
        f.touch()
        handler.dispatch(FileOpenedEvent(str(f)))
        mock_updater.ingestor.execute_write.assert_not_called()
        mock_updater.ingestor.flush_all.assert_not_called()

    def test_closed_no_write_event_is_ignored(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "viewed.py"
        f.touch()
        handler.dispatch(FileClosedNoWriteEvent(str(f)))
        mock_updater.ingestor.execute_write.assert_not_called()
        mock_updater.ingestor.flush_all.assert_not_called()

    def test_access_event_is_ignored(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "accessed.py"
        f.touch()
        ev = _make_event("access", str(f))
        handler.dispatch(ev)
        mock_updater.ingestor.execute_write.assert_not_called()
        mock_updater.ingestor.flush_all.assert_not_called()


class TestNonCodeFileHandling:
    def test_markdown_file_creates_file_node(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "readme.md"
        f.write_text("# Title", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        mock_updater.factory.structure_processor.process_generic_file.assert_called_once_with(
            f, "readme.md"
        )

    def test_json_file_creates_file_node(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "config.json"
        f.write_text("{}", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        mock_updater.factory.structure_processor.process_generic_file.assert_called_once_with(
            f, "config.json"
        )

    def test_non_code_file_deletion_removes_file_node(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "notes.md"
        handler.dispatch(FileDeletedEvent(str(f)))
        delete_file_calls = [
            c
            for c in mock_updater.ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert len(delete_file_calls) == 1
        assert delete_file_calls[0].args[1] == {
            cs.KEY_PATH: f.resolve().as_posix(),
        }
        mock_updater.factory.structure_processor.process_generic_file.assert_not_called()

    def test_non_code_file_has_no_module_node(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "data.md"
        f.write_text("text", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        mock_updater.factory.definition_processor.process_file.assert_not_called()


class TestSecondaryTierReparse:
    # Step 1 of a change always deletes the file's Module and everything
    # hanging off it (DEFINES for the ast-grep tier, CONTAINS_SECTION for the
    # document tier). If Step 3 does not re-parse, the edit EMPTIES the file
    # in the graph rather than refreshing it, while still logging success
    # (issue #1427). Tree-sitter languages have always re-parsed; these tiers
    # did not.
    def test_markdown_edit_reparses_through_the_document_tier(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        """A watched Markdown edit re-parses instead of losing its Sections."""
        f = temp_repo / "plan.md"
        f.write_text("# Heading\n", encoding="utf-8")
        handler.dispatch(FileModifiedEvent(str(f)))
        mock_updater.process_with_secondary_tier.assert_called_once_with(f)

    def test_ruby_edit_reparses_through_the_secondary_tier(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        """A watched Ruby edit re-parses instead of losing its definitions."""
        f = temp_repo / "app.rb"
        f.write_text("def hi\n  1\nend\n", encoding="utf-8")
        handler.dispatch(FileModifiedEvent(str(f)))
        mock_updater.process_with_secondary_tier.assert_called_once_with(f)

    def test_created_file_also_reparses(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        """A newly created file is parsed by its tier, not only deleted-and-skipped."""
        f = temp_repo / "new.md"
        f.write_text("# New\n", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))
        mock_updater.process_with_secondary_tier.assert_called_once_with(f)

    def test_tree_sitter_file_does_not_take_the_secondary_path(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        """A tree-sitter file takes only its own branch, so it is parsed once.

        Python is handled by definition_processor; routing it through the
        secondary tiers as well would parse it twice.
        """
        f = temp_repo / "mod.py"
        f.write_text("def f():\n    return 1\n", encoding="utf-8")
        handler.dispatch(FileModifiedEvent(str(f)))
        mock_updater.factory.definition_processor.process_file.assert_called_once()
        mock_updater.process_with_secondary_tier.assert_not_called()

    def test_deletion_does_not_reparse(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        """A delete has nothing to re-parse, so no tier runs for it."""
        f = temp_repo / "gone.md"
        handler.dispatch(FileDeletedEvent(str(f)))
        mock_updater.process_with_secondary_tier.assert_not_called()


class TestMixedEventSequences:
    def test_rapid_create_modify_delete(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f = temp_repo / "ephemeral.py"
        f.write_text("a = 1", encoding="utf-8")
        handler.dispatch(FileCreatedEvent(str(f)))

        mock_updater.ingestor.reset_mock()
        mock_updater.factory.reset_mock()
        f.write_text("a = 2", encoding="utf-8")
        handler.dispatch(FileModifiedEvent(str(f)))

        mock_updater.ingestor.reset_mock()
        mock_updater.factory.reset_mock()
        handler.dispatch(FileDeletedEvent(str(f)))

        # After delete, no re-parse or file node creation
        mock_updater.factory.definition_processor.process_file.assert_not_called()
        mock_updater.factory.structure_processor.process_generic_file.assert_not_called()
        assert mock_updater.ingestor.execute_write.call_count == 3
        mock_updater.ingestor.flush_all.assert_called_once()

    def test_multiple_files_changed(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f1 = temp_repo / "a.py"
        f2 = temp_repo / "b.py"
        f1.write_text("x = 1", encoding="utf-8")
        f2.write_text("y = 2", encoding="utf-8")

        handler.dispatch(FileModifiedEvent(str(f1)))
        handler.dispatch(FileModifiedEvent(str(f2)))

        assert mock_updater.ingestor.execute_write.call_count == 6
        assert mock_updater.ingestor.flush_all.call_count == 2


class TestCypherDeleteFileQuery:
    def test_delete_file_only_targets_specific_path(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        f1 = temp_repo / "keep.py"
        f2 = temp_repo / "remove.py"
        f1.write_text("a = 1", encoding="utf-8")

        handler.dispatch(FileDeletedEvent(str(f2)))

        delete_file_calls = [
            c
            for c in mock_updater.ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_FILE
        ]
        assert len(delete_file_calls) == 1
        assert delete_file_calls[0].args[1] == {cs.KEY_PATH: f2.resolve().as_posix()}

        delete_module_calls = [
            c
            for c in mock_updater.ingestor.execute_write.call_args_list
            if c.args[0] == cs.CYPHER_DELETE_MODULE
        ]
        assert len(delete_module_calls) == 1
        # The module delete is project-scoped: the query requires the scope
        # parameters or a realtime change fails the delete outright.
        assert delete_module_calls[0].args[1] == {
            cs.KEY_PATH: "remove.py",
            cs.KEY_PROJECT_NAME: mock_updater.project_name,
            cs.KEY_PROJECT_PREFIX: f"{mock_updater.project_name}.",
        }
