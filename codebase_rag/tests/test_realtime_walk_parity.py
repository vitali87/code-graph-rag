from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import DirMovedEvent, FileModifiedEvent

from codebase_rag import constants as cs
from codebase_rag.utils.path_utils import walk_eligible_files
from realtime_updater import CodeChangeEventHandler

# The watcher and the repository walk must agree about which files are in the
# graph. The watcher used to check directory names against the built-in
# ignores only, so it dropped every change under a directory the run had
# unignored and followed changes under one the user had excluded.

_TREE = [
    "app.py",
    "vendor/lib/util.py",
    "node_modules/pkg/index.js",
    "docs/guide.py",
    "src/bin/main.rs",
    "bin/tool.py",
    "pkg/build/gen.py",
    "web/jquery.min.js",
    cs.HASH_CACHE_FILENAME,
]


def _write(repo: Path, rel: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    return path


@pytest.fixture
def handler(mock_updater: MagicMock) -> CodeChangeEventHandler:
    return CodeChangeEventHandler(mock_updater, debounce_seconds=0)


@pytest.mark.parametrize(
    ("exclude_paths", "unignore_paths"),
    [
        (None, None),
        (frozenset({"docs/"}), None),
        (None, frozenset({"vendor"})),
        (frozenset({"docs/"}), frozenset({"vendor", "pkg/build/gen.py"})),
    ],
)
def test_the_watcher_accepts_exactly_the_files_the_walk_indexes(
    handler: CodeChangeEventHandler,
    mock_updater: MagicMock,
    temp_repo: Path,
    exclude_paths: frozenset[str] | None,
    unignore_paths: frozenset[str] | None,
) -> None:
    for rel in _TREE:
        _write(temp_repo, rel)
    mock_updater.exclude_paths = exclude_paths
    mock_updater.unignore_paths = unignore_paths
    walked = {
        rel
        for _dir, _name, rel in walk_eligible_files(
            temp_repo, exclude_paths=exclude_paths, unignore_paths=unignore_paths
        )
    }
    watched = {rel for rel in _TREE if handler._is_relevant(str(temp_repo / rel))}
    assert watched == walked


def test_an_edit_under_an_unignored_directory_reaches_the_graph(
    handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    path = _write(temp_repo, "vendor/lib/util.py")
    mock_updater.unignore_paths = frozenset({"vendor"})
    handler.dispatch(FileModifiedEvent(str(path)))
    mock_updater.reingest.assert_called_once_with((path,))


def test_an_edit_under_an_excluded_directory_is_dropped(
    handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    path = _write(temp_repo, "docs/guide.py")
    mock_updater.exclude_paths = frozenset({"docs/"})
    handler.dispatch(FileModifiedEvent(str(path)))
    mock_updater.reingest.assert_not_called()


def test_the_indexers_own_state_files_are_not_changes(
    handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    # Every run rewrites the hash cache in the repository root; reacting to
    # that write re-ingested a file the graph never holds.
    path = _write(temp_repo, cs.HASH_CACHE_FILENAME)
    handler.dispatch(FileModifiedEvent(str(path)))
    mock_updater.reingest.assert_not_called()


def test_a_directory_moved_into_an_unignored_path_is_indexed(
    handler: CodeChangeEventHandler, mock_updater: MagicMock, temp_repo: Path
) -> None:
    moved = _write(temp_repo, "vendor/lib/util.py")
    mock_updater.unignore_paths = frozenset({"vendor"})
    mock_updater.indexed_files_under.return_value = []
    handler.dispatch(
        DirMovedEvent(str(temp_repo / "lib"), str(temp_repo / "vendor" / "lib"))
    )
    mock_updater.reingest.assert_called_once_with((moved,))
