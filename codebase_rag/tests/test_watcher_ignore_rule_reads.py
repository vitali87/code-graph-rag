"""Issue #3583: a read of an ignore file no longer re-reads the ignore rules.

The watcher re-read its rules on any event naming `.gitignore`, `.cgrignore`
or the git index, reads included. Re-reading them opens those same files, so
the first `git status` (VS Code's Source Control panel runs one) started a
loop that re-read `.gitignore` every few milliseconds, for good. Only a
change to an input re-reads the rules now.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import (
    FileClosedEvent,
    FileClosedNoWriteEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileOpenedEvent,
    FileSystemEvent,
)
from watchdog.observers import Observer

from codebase_rag import config, watch
from codebase_rag.config import CGRIGNORE_FILENAME, GITIGNORE_FILENAME
from codebase_rag.types_defs import CgrignorePatterns

INPUTS = (GITIGNORE_FILENAME, CGRIGNORE_FILENAME, ".git/index")


class _CountingRules:
    """Ignore rules that count how often the handler re-reads them."""

    def __init__(self, repo: Path) -> None:
        self.repo_path = repo
        self.inputs = frozenset(repo / name for name in INPUTS)
        self.reloads = 0

    def reload(self) -> bool:
        self.reloads += 1
        return False

    def build_updater(self) -> MagicMock:
        raise AssertionError("unchanged rules build no updater")


def _updater_double(repo: Path) -> MagicMock:
    return MagicMock(
        repo_path=repo, project_name="p", exclude_paths=None, unignore_paths=None
    )


def _handler(repo: Path, rules: _CountingRules) -> watch.CodeChangeEventHandler:
    return watch.CodeChangeEventHandler(
        _updater_double(repo), debounce_seconds=0, ignore_rules=rules
    )


@pytest.mark.parametrize("name", INPUTS)
@pytest.mark.parametrize(
    "event_cls", [FileOpenedEvent, FileClosedNoWriteEvent], ids=["opened", "read"]
)
def test_a_read_of_an_input_keeps_the_rules(
    tmp_path: Path, name: str, event_cls: type[FileSystemEvent]
) -> None:
    rules = _CountingRules(tmp_path)

    _handler(tmp_path, rules).dispatch(event_cls(str(tmp_path / name)))

    assert rules.reloads == 0


@pytest.mark.parametrize("name", INPUTS)
@pytest.mark.parametrize(
    "event_cls",
    [FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, FileClosedEvent],
    ids=["modified", "created", "deleted", "closed"],
)
def test_a_change_to_an_input_still_rereads_the_rules(
    tmp_path: Path, name: str, event_cls: type[FileSystemEvent]
) -> None:
    # Negative: edits, `git add` and friends still move the rules.
    rules = _CountingRules(tmp_path)

    _handler(tmp_path, rules).dispatch(event_cls(str(tmp_path / name)))

    assert rules.reloads == 1


def test_a_rename_onto_the_index_still_rereads_the_rules(tmp_path: Path) -> None:
    # git writes `index.lock` and renames it over the index.
    rules = _CountingRules(tmp_path)
    git_dir = tmp_path / ".git"

    _handler(tmp_path, rules).dispatch(
        FileMovedEvent(str(git_dir / "index.lock"), str(git_dir / "index"))
    )

    assert rules.reloads == 1


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="only inotify reports reads of a file",
)
def test_a_read_under_a_real_watch_does_not_start_a_reload_loop(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The issue as reported, on the real observer: one read of `.gitignore`
    # re-read it hundreds of times a second, because each re-read raised the
    # events of the next one.
    gitignore = git_repo / GITIGNORE_FILENAME
    gitignore.write_text("dist/\n")
    loads: list[Path] = []
    real_load = config._load_ignore_file

    def counting_load(path: Path) -> CgrignorePatterns:
        loads.append(path)
        return real_load(path)

    monkeypatch.setattr(config, "_load_ignore_file", counting_load)
    rules = watch.IgnoreRules(git_repo, lambda _patterns: _updater_double(git_repo))
    loads.clear()
    read_seen = threading.Event()

    class _Handler(watch.CodeChangeEventHandler):
        def dispatch(self, event: FileSystemEvent) -> None:
            super().dispatch(event)
            if (
                event.event_type == FileClosedNoWriteEvent.event_type
                and Path(watch._event_path(event.src_path)) == gitignore
            ):
                read_seen.set()

    handler = _Handler(
        _updater_double(git_repo), debounce_seconds=0, ignore_rules=rules
    )
    observer = Observer()
    observer.schedule(handler, str(git_repo), recursive=True)
    observer.start()
    try:
        gitignore.read_text()
        # Events are handled in order on one thread, so once the read's last
        # event is through, any reload it set off has already happened.
        assert read_seen.wait(timeout=10), "the watch never reported the read"
    finally:
        observer.stop()
        observer.join()

    assert loads == []
