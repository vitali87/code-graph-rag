"""Issue #2448: a directory kept with `--interactive-setup` stays kept.

The choice applied to that run only: the next ordinary sync (or the watcher,
an MCP reingest, CI) read the exclusions from `.cgrignore` alone, saw a
changed set and removed the kept directory from the graph, with nothing at
the prompt saying the choice was per-run. The prompt now offers to save new
keeps to `.cgrignore` as `!path` lines, the documented way to unignore, and
says plainly when they apply to this run only.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.config import CGRIGNORE_FILENAME, load_ignore_patterns
from codebase_rag.main import prompt_for_unignored_directories


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "cli.py").write_text("def main():\n    pass\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.py").write_text("def lib():\n    pass\n")
    return tmp_path


@pytest.fixture
def console() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.main.app_context") as context:
        yield context.console


def _printed(console: MagicMock) -> str:
    return " ".join(str(call.args[0]) for call in console.print.call_args_list)


def _keep(repo: Path, answer: str, confirm: bool) -> tuple[frozenset[str], MagicMock]:
    with (
        patch("codebase_rag.main.Prompt.ask", return_value=answer),
        patch("rich.prompt.Confirm.ask", return_value=confirm) as confirm_ask,
    ):
        kept = prompt_for_unignored_directories(repo)
    return kept, confirm_ask


def test_a_kept_directory_is_saved_for_later_syncs(
    repo: Path, console: MagicMock
) -> None:
    kept, confirm_ask = _keep(repo, "1", confirm=True)

    assert "bin" in kept
    confirm_ask.assert_called_once()
    # What an ordinary sync reads: the choice is now part of it.
    assert "bin" in load_ignore_patterns(repo).unignore
    assert "!bin" in (repo / CGRIGNORE_FILENAME).read_text().splitlines()


def test_declining_keeps_it_for_this_run_and_says_how_to_keep_it(
    repo: Path, console: MagicMock
) -> None:
    kept, _ = _keep(repo, "1", confirm=False)

    assert "bin" in kept
    assert not (repo / CGRIGNORE_FILENAME).exists()
    printed = _printed(console)
    assert "this run" in printed and "!bin" in printed


def test_existing_cgrignore_lines_are_kept(repo: Path, console: MagicMock) -> None:
    (repo / CGRIGNORE_FILENAME).write_text("vendor")

    _keep(repo, "1", confirm=True)

    lines = (repo / CGRIGNORE_FILENAME).read_text().splitlines()
    assert lines[0] == "vendor" and "!bin" in lines
    assert load_ignore_patterns(repo).exclude >= {"vendor"}


def test_keeping_nothing_asks_nothing(repo: Path, console: MagicMock) -> None:
    # Negative.
    kept, confirm_ask = _keep(repo, "none", confirm=True)

    assert "bin" not in kept
    confirm_ask.assert_not_called()
    assert not (repo / CGRIGNORE_FILENAME).exists()


def test_a_choice_already_saved_is_not_asked_again(
    repo: Path, console: MagicMock
) -> None:
    # Negative: nothing new to save, so no question and no duplicate line.
    (repo / CGRIGNORE_FILENAME).write_text("!bin\n")

    kept, confirm_ask = _keep(repo, "1", confirm=True)

    assert "bin" in kept
    confirm_ask.assert_not_called()
    assert (repo / CGRIGNORE_FILENAME).read_text() == "!bin\n"
