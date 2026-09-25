from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

import realtime_updater
from codebase_rag.tests.conftest import create_and_run_updater
from realtime_updater import CodeChangeEventHandler

# A move is a deletion of its source plus a creation of its destination, and a
# directory event stands for every file beneath it. Before these were handled
# the watcher dropped all of them: atomic saves and renames never reached the
# graph, and a deleted directory's nodes stayed in it for good.


@pytest.fixture
def handler(mock_updater: MagicMock) -> CodeChangeEventHandler:
    h = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
    h.ignore_patterns = h.ignore_patterns - {"tmp", "temp"}
    return h


def _write(path: Path, text: str = "x = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestFileMoves:
    def test_atomic_save_over_target_reingests_target(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        # The editor writes a temp file then renames it over the target; the
        # temp source is ignored, so only the target is re-parsed.
        target = _write(temp_repo / "app.py", "x = 2\n")
        handler.dispatch(FileMovedEvent(str(temp_repo / "app.py.tmp"), str(target)))
        mock_updater.reingest.assert_called_once_with((target,))

    def test_rename_removes_source_and_indexes_destination(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        src = temp_repo / "a.py"
        dest = _write(temp_repo / "b.py")
        handler.dispatch(FileMovedEvent(str(src), str(dest)))
        assert mock_updater.reingest.call_args_list == [
            call((), deleted=(src,)),
            call((dest,)),
        ]

    def test_move_into_ignored_directory_is_a_deletion(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        src = temp_repo / "a.py"
        dest = _write(temp_repo / "node_modules" / "a.py")
        handler.dispatch(FileMovedEvent(str(src), str(dest)))
        mock_updater.reingest.assert_called_once_with((), deleted=(src,))

    def test_move_out_of_ignored_directory_is_a_creation(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        dest = _write(temp_repo / "a.py")
        handler.dispatch(
            FileMovedEvent(str(temp_repo / "node_modules" / "a.py"), str(dest))
        )
        mock_updater.reingest.assert_called_once_with((dest,))

    def test_debounced_rename_processes_both_sides(
        self, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        timers: list[MagicMock] = []

        def factory(
            _delay: float, fn: Callable[[str], None], args: list[str]
        ) -> MagicMock:
            timer = MagicMock()
            timer.start.side_effect = lambda: timers.append(timer)
            timer.fire = lambda: fn(*args)
            return timer

        h = CodeChangeEventHandler(mock_updater, timer_factory=factory)
        src = temp_repo / "a.py"
        dest = _write(temp_repo / "b.py")
        h.dispatch(FileMovedEvent(str(src), str(dest)))
        for timer in timers:
            timer.fire()
        assert mock_updater.reingest.call_args_list == [
            call((), deleted=(src,)),
            call((dest,)),
        ]


class TestDirectoryEvents:
    def test_directory_deletion_removes_indexed_files_under_it(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        gone = [temp_repo / "pkg" / "a.py", temp_repo / "pkg" / "sub" / "b.py"]
        mock_updater.indexed_files_under.return_value = gone
        handler.dispatch(DirDeletedEvent(str(temp_repo / "pkg")))
        mock_updater.indexed_files_under.assert_called_once_with(temp_repo / "pkg")
        assert mock_updater.reingest.call_args_list == [
            call((), deleted=(path,)) for path in gone
        ]

    def test_directory_move_deletes_old_files_and_indexes_new_ones(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        old = temp_repo / "old" / "a.py"
        new = _write(temp_repo / "new" / "a.py")
        mock_updater.indexed_files_under.return_value = [old]
        handler.dispatch(DirMovedEvent(str(temp_repo / "old"), str(temp_repo / "new")))
        assert mock_updater.reingest.call_args_list == [
            call((), deleted=(old,)),
            call((new,)),
        ]

    def test_directory_moved_into_ignored_path_only_deletes(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        old = temp_repo / "pkg" / "a.py"
        _write(temp_repo / "node_modules" / "pkg" / "a.py")
        mock_updater.indexed_files_under.return_value = [old]
        handler.dispatch(
            DirMovedEvent(
                str(temp_repo / "pkg"), str(temp_repo / "node_modules" / "pkg")
            )
        )
        mock_updater.reingest.assert_called_once_with((), deleted=(old,))

    def test_directory_created_is_left_to_its_file_events(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        (temp_repo / "pkg").mkdir()
        handler.dispatch(DirCreatedEvent(str(temp_repo / "pkg")))
        mock_updater.reingest.assert_not_called()

    def test_synthetic_moves_of_a_moved_directorys_children_are_skipped(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        # The directory's own move already restated its files; watchdog's
        # follow-up moves of each child must not re-ingest them again.
        new = _write(temp_repo / "new" / "a.py")
        handler.dispatch(
            FileMovedEvent(str(temp_repo / "old" / "a.py"), str(new), is_synthetic=True)
        )
        handler.dispatch(
            DirMovedEvent(
                str(temp_repo / "old" / "sub"),
                str(temp_repo / "new" / "sub"),
                is_synthetic=True,
            )
        )
        mock_updater.reingest.assert_not_called()
        mock_updater.indexed_files_under.assert_not_called()

    def test_directory_move_does_not_walk_ignored_subdirectories(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        new = _write(temp_repo / "new" / "a.py")
        _write(temp_repo / "new" / "node_modules" / "dep" / "index.js")
        mock_updater.indexed_files_under.return_value = []
        walked: list[str] = []
        real_walk = realtime_updater.os.walk

        def recording_walk(
            top: str, *args: bool, **kwargs: bool
        ) -> Iterator[tuple[str, list[str], list[str]]]:
            for entry in real_walk(top, *args, **kwargs):
                walked.append(entry[0])
                yield entry

        # Patched only around the dispatch: `os` is shared, and on Windows the
        # temp_repo teardown's rmtree walks with `os.walk` too.
        with patch.object(realtime_updater.os, "walk", recording_walk):
            handler.dispatch(
                DirMovedEvent(str(temp_repo / "old"), str(temp_repo / "new"))
            )
        mock_updater.reingest.assert_called_once_with((new,))
        assert walked == [str(temp_repo / "new")]

    def test_directory_moved_under_an_ignored_path_is_not_walked(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        _write(temp_repo / "node_modules" / "pkg" / "a.py")
        mock_updater.indexed_files_under.return_value = []
        with patch.object(realtime_updater.os, "walk") as walk:
            handler.dispatch(
                DirMovedEvent(
                    str(temp_repo / "pkg"), str(temp_repo / "node_modules" / "pkg")
                )
            )
        walk.assert_not_called()
        mock_updater.reingest.assert_not_called()

    def test_directory_deletion_reported_as_a_file_still_removes_its_files(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        # Windows' observer cannot tell a deleted directory from a file.
        gone = [temp_repo / "pkg" / "a.py", temp_repo / "pkg" / "b.py"]
        mock_updater.indexed_files_under.return_value = gone
        handler.dispatch(FileDeletedEvent(str(temp_repo / "pkg")))
        assert mock_updater.reingest.call_args_list == [
            call((), deleted=(path,)) for path in gone
        ]

    def test_file_deletion_is_still_a_deletion_of_that_file(
        self, handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
    ) -> None:
        mock_updater.indexed_files_under.return_value = []
        handler.dispatch(FileDeletedEvent(str(temp_repo / "a.py")))
        mock_updater.reingest.assert_called_once_with((), deleted=(temp_repo / "a.py",))


class TestIndexedFilesUnder:
    def test_reads_the_files_the_last_run_indexed(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(temp_repo / "pkg" / "__init__.py", "")
        _write(temp_repo / "pkg" / "a.py")
        _write(temp_repo / "pkg" / "sub" / "b.py")
        _write(temp_repo / "pkgx" / "c.py")
        _write(temp_repo / "main.py")
        updater = create_and_run_updater(temp_repo, mock_ingestor)
        shutil.rmtree(temp_repo / "pkg")
        # The sibling `pkgx` shares the prefix string but not the directory.
        assert updater.indexed_files_under(temp_repo / "pkg") == [
            temp_repo / "pkg" / "__init__.py",
            temp_repo / "pkg" / "a.py",
            temp_repo / "pkg" / "sub" / "b.py",
        ]

    @pytest.mark.parametrize("where", ["outside", "root"])
    def test_a_directory_that_is_not_inside_the_repo_names_no_files(
        self, temp_repo: Path, mock_ingestor: MagicMock, tmp_path: Path, where: str
    ) -> None:
        _write(temp_repo / "main.py")
        updater = create_and_run_updater(temp_repo, mock_ingestor)
        # The repo root itself is not a subdirectory whose files went away.
        directory = tmp_path / "elsewhere" if where == "outside" else temp_repo
        assert updater.indexed_files_under(directory) == []
