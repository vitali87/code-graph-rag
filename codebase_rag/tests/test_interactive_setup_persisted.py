"""Issue #2448: a directory kept with `--interactive-setup` stays kept.

The choice applied to that run only: the next ordinary sync (or the watcher,
an MCP reingest, CI) read the exclusions from `.cgrignore` alone, saw a
changed set and removed the kept directory from the graph, with nothing at
the prompt saying the choice was per-run. The prompt now offers to save new
keeps to `.cgrignore` as `!path` lines, the documented way to unignore, and
says plainly when they apply to this run only.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.config import CGRIGNORE_FILENAME, load_ignore_patterns
from codebase_rag.main import prompt_for_unignored_directories
from codebase_rag.utils.path_utils import (
    is_eligible_rel_file,
    should_skip_path,
    walk_eligible_files,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    # A subdirectory, so a test can place a file outside the repository.
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "cli.py").write_text("def main():\n    pass\n")
    (root / "src").mkdir()
    (root / "src" / "lib.py").write_text("def lib():\n    pass\n")
    return root


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
    assert "this run" in printed
    assert "!bin" in printed


def test_existing_cgrignore_lines_are_kept(repo: Path, console: MagicMock) -> None:
    (repo / CGRIGNORE_FILENAME).write_text("vendor")

    _keep(repo, "1", confirm=True)

    lines = (repo / CGRIGNORE_FILENAME).read_text().splitlines()
    assert lines[0] == "vendor"
    assert "!bin" in lines
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


def _skipped_by_a_later_sync(repo: Path, rel_path: str) -> bool:
    # The skip decision an ordinary sync makes from the saved files alone.
    patterns = load_ignore_patterns(repo)
    return should_skip_path(
        repo / rel_path,
        repo,
        patterns.exclude or None,
        patterns.unignore or None,
        is_file=True,
    )


@pytest.fixture
def vendored(repo: Path) -> Path:
    (repo / "vendor").mkdir()
    (repo / "vendor" / "lib.py").write_text("def vendored():\n    pass\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.py").write_text("X = 1\n")
    (repo / CGRIGNORE_FILENAME).write_text("# third-party\nvendor\nbuild\n")
    return repo


def test_keeping_a_cgrignore_exclusion_lifts_it_for_later_syncs(
    vendored: Path, console: MagicMock
) -> None:
    # Review of PR 2510: a .cgrignore exclude beats any `!` line, so saving
    # `!vendor` beside `vendor` left the next sync excluding it anyway while
    # the prompt said every later sync keeps it. The exclude line goes.
    assert _skipped_by_a_later_sync(vendored, "vendor/lib.py")

    _keep(vendored, "all", confirm=True)

    assert not _skipped_by_a_later_sync(vendored, "vendor/lib.py")
    assert not _skipped_by_a_later_sync(vendored, "build/out.py")
    lines = (vendored / CGRIGNORE_FILENAME).read_text().splitlines()
    assert "vendor" not in lines
    assert "build" not in lines
    assert lines[0] == "# third-party"


def test_an_exclusion_that_is_not_kept_stays(
    vendored: Path, console: MagicMock
) -> None:
    # Negative: only the kept exclusion is lifted. Rows: bin, build, vendor.
    _keep(vendored, "3", confirm=True)

    assert not _skipped_by_a_later_sync(vendored, "vendor/lib.py")
    assert _skipped_by_a_later_sync(vendored, "build/out.py")
    assert "build" in (vendored / CGRIGNORE_FILENAME).read_text().splitlines()


def test_declining_to_lift_an_exclusion_says_it_stays_excluded(
    vendored: Path, console: MagicMock
) -> None:
    before = (vendored / CGRIGNORE_FILENAME).read_text()

    _keep(vendored, "3", confirm=False)

    assert (vendored / CGRIGNORE_FILENAME).read_text() == before
    assert _skipped_by_a_later_sync(vendored, "vendor/lib.py")
    printed = _printed(console)
    assert "vendor" in printed
    assert "still excluded" in printed


def test_this_run_honours_a_lifted_exclusion(
    vendored: Path, console: MagicMock
) -> None:
    # The run that saved the choice keeps the directory too, not only the
    # next one.
    with (
        patch("codebase_rag.cli.connect_memgraph") as connect,
        patch("codebase_rag.graph_updater.GraphUpdater") as updater,
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.main.Prompt.ask", return_value="3"),
        patch("rich.prompt.Confirm.ask", return_value=True),
    ):
        connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        connect.return_value.__exit__ = MagicMock(return_value=False)
        result = CliRunner().invoke(
            app,
            [
                "start",
                "--update-graph",
                "--interactive-setup",
                "--repo-path",
                str(vendored),
            ],
        )

    assert result.exit_code == 0, result.output
    excluded = updater.call_args.kwargs["exclude_paths"] or frozenset()
    assert "vendor" not in excluded
    assert "build" in excluded


def test_a_failed_write_leaves_the_existing_rules_intact(
    vendored: Path, console: MagicMock
) -> None:
    # Review of PR 2510: writing .cgrignore in place truncated it first, so a
    # write that failed part-way (a full disk) lost the existing rules.
    before = (vendored / CGRIGNORE_FILENAME).read_text()
    real_write_text = Path.write_text
    real_fdopen = os.fdopen

    def disk_fills(self: Path, data: str, *args: object, **kwargs: object) -> int:
        real_write_text(self, "", encoding="utf-8")
        raise OSError(errno.ENOSPC, "No space left on device")

    class _FullDisk:
        def __init__(self, fd: int, *args: object, **kwargs: object) -> None:
            self._handle = real_fdopen(fd, "w", encoding="utf-8")

        def __enter__(self) -> _FullDisk:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

        def write(self, data: str) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    with (
        patch.object(Path, "write_text", disk_fills),
        patch("codebase_rag.main.os.fdopen", _FullDisk),
    ):
        _keep(vendored, "3", confirm=True)

    assert (vendored / CGRIGNORE_FILENAME).read_text() == before
    # No temp file left behind; the write lock's own file is cgr state.
    assert sorted(
        p.name
        for p in vendored.iterdir()
        if p.is_file() and p.name not in cs.CGR_STATE_FILENAMES
    ) == [CGRIGNORE_FILENAME]
    assert "Could not write" in _printed(console)


@pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks needs privileges"
)
def test_a_planted_temp_file_link_is_never_written_through(
    vendored: Path, console: MagicMock, tmp_path: Path
) -> None:
    # Review of PR 2510 (CWE-377): the temp name was fixed, so a repository
    # that ships `.cgrignore.tmp` as a link made the save overwrite the file
    # it points at, anywhere the user can write.
    outside = tmp_path / "elsewhere" / "precious.txt"
    outside.parent.mkdir()
    outside.write_text("precious\n")
    (vendored / f"{CGRIGNORE_FILENAME}.tmp").symlink_to(outside)

    _keep(vendored, "3", confirm=True)

    assert outside.read_text() == "precious\n"
    saved = vendored / CGRIGNORE_FILENAME
    assert not saved.is_symlink()
    assert "!vendor" in saved.read_text().splitlines()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_saved_file_keeps_its_permissions(
    vendored: Path, console: MagicMock
) -> None:
    # Negative: the rewrite does not narrow the file to the temp file's 0600.
    (vendored / CGRIGNORE_FILENAME).chmod(0o640)

    _keep(vendored, "3", confirm=True)

    assert stat.S_IMODE((vendored / CGRIGNORE_FILENAME).stat().st_mode) == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_new_file_is_readable_like_any_checkout_file(
    repo: Path, console: MagicMock
) -> None:
    # Negative: a .cgrignore the save creates is 0644, not the temp's 0600.
    _keep(repo, "1", confirm=True)

    assert stat.S_IMODE((repo / CGRIGNORE_FILENAME).stat().st_mode) == 0o644


def _walked(repo: Path) -> set[str]:
    # What an ordinary sync indexes: the real repository walk, fed the
    # patterns it reads from the saved files alone.
    patterns = load_ignore_patterns(repo)
    return {
        rel
        for _, _, rel in walk_eligible_files(
            repo, patterns.exclude or None, patterns.unignore or None
        )
    }


@pytest.fixture
def generated(repo: Path) -> Path:
    # `.gitignore` excludes the directory a kept one sits in.
    (repo / ".gitignore").write_text("generated/\n")
    for rel in (
        "generated/node_modules/pkg/index.js",
        "generated/out.py",
        "generated/other/gen.py",
    ):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("X = 1\n")
    return repo


def test_a_keep_inside_an_excluded_directory_is_walked(
    generated: Path, console: MagicMock
) -> None:
    # Greptile review of PR 2510: `!generated/node_modules` was saved, but the
    # walk pruned `generated/` before reaching it, so the kept files stayed
    # out on this run and every later one. Rows: bin, generated, node_modules.
    assert "generated/node_modules/pkg/index.js" not in _walked(generated)

    kept, _ = _keep(generated, "3", confirm=True)

    assert kept >= {"generated/node_modules"}
    walked = _walked(generated)
    assert "generated/node_modules/pkg/index.js" in walked
    # The rest of the excluded directory stays out.
    assert "generated/out.py" not in walked
    assert "generated/other/gen.py" not in walked
    assert "src/lib.py" in walked
    # The watcher asks the same question one path at a time.
    patterns = load_ignore_patterns(generated)
    assert is_eligible_rel_file(
        "generated/node_modules/pkg/index.js", patterns.exclude, patterns.unignore
    )
    assert not is_eligible_rel_file(
        "generated/out.py", patterns.exclude, patterns.unignore
    )
    # Structure traversal enters the enclosing directory, so the kept files
    # get their Folder ancestry, and still skips its other children.
    assert not should_skip_path(
        generated / "generated",
        generated,
        patterns.exclude,
        patterns.unignore,
        is_file=False,
    )
    assert should_skip_path(
        generated / "generated" / "other",
        generated,
        patterns.exclude,
        patterns.unignore,
        is_file=False,
    )


def test_a_keep_with_no_excluded_ancestor_behaves_as_before(
    generated: Path, console: MagicMock
) -> None:
    # Negative: keeping `bin` lifts nothing else, and `generated/` stays out.
    _keep(generated, "1", confirm=True)

    walked = _walked(generated)
    assert "bin/cli.py" in walked
    assert not any(rel.startswith("generated/") for rel in walked)


def test_an_excluded_file_inside_a_kept_directory_stays_excluded(
    generated: Path, console: MagicMock
) -> None:
    # Negative: only the exclusion of the ENCLOSING directory gives way; a
    # pattern naming files inside the kept one still wins.
    (generated / ".gitignore").write_text("generated/\n*.js\n")

    _keep(generated, "3", confirm=True)

    assert "generated/node_modules/pkg/index.js" not in _walked(generated)


def test_concurrent_saves_both_keep_their_choice(
    vendored: Path, console: MagicMock
) -> None:
    # Greptile review of PR 2510: two setups each read `.cgrignore` before
    # either replaced it, so the later write dropped the other's keep. The
    # first save stops mid-write until the second has run (or, holding the
    # lock, until it is clear the second is waiting for it).
    import threading

    from codebase_rag import main as main_module

    real_replace = main_module._replace_ignore_file
    first_writing = threading.Event()
    second_done = threading.Event()

    def stalled_replace(ignore_file: Path, content: str) -> None:
        if not first_writing.is_set():
            first_writing.set()
            second_done.wait(timeout=1.0)
        real_replace(ignore_file, content)

    def save(keep: str) -> None:
        main_module._offer_to_save_keeps(vendored, frozenset({keep}), frozenset())

    def second() -> None:
        first_writing.wait(timeout=5.0)
        save("docs")
        second_done.set()

    with (
        patch.object(main_module, "_replace_ignore_file", stalled_replace),
        patch("rich.prompt.Confirm.ask", return_value=True),
    ):
        other = threading.Thread(target=second)
        other.start()
        save("tools")
        other.join(timeout=10.0)

    assert not other.is_alive()
    unignore = load_ignore_patterns(vendored).unignore
    assert {"tools", "docs"} <= unignore


def _walked_by_cli(repo: Path, command: list[str]) -> set[str]:
    # The files the CLI's own run would index: the exclude and unignore sets
    # it hands GraphUpdater, fed to the real walk.
    with (
        patch("codebase_rag.cli.connect_memgraph") as connect,
        patch("codebase_rag.graph_updater.GraphUpdater") as updater,
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
    ):
        connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        connect.return_value.__exit__ = MagicMock(return_value=False)
        result = CliRunner().invoke(app, [*command, "--repo-path", str(repo)])
    assert result.exit_code == 0, result.output
    kwargs = updater.call_args.kwargs
    return {
        rel
        for _, _, rel in walk_eligible_files(
            repo, kwargs["exclude_paths"], kwargs["unignore_paths"]
        )
    }


@pytest.fixture
def saved_nested_keep(repo: Path) -> Path:
    (repo / CGRIGNORE_FILENAME).write_text("!generated/node_modules\n")
    for rel in ("generated/node_modules/kept.py", "generated/out.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("X = 1\n")
    return repo


@pytest.mark.parametrize(
    "command",
    [
        ["start", "--update-graph"],
        ["index", "-o", "out"],
    ],
)
def test_a_run_level_exclusion_beats_a_saved_nested_keep(
    saved_nested_keep: Path, tmp_path: Path, command: list[str]
) -> None:
    # Greptile review of PR 2510: `--exclude generated` is this run's own
    # choice, so a keep saved beneath it in `.cgrignore` does not reopen it.
    if command[0] == "index":
        command = ["index", "-o", str(tmp_path / "out")]

    walked = _walked_by_cli(saved_nested_keep, [*command, "--exclude", "generated"])

    assert not any(rel.startswith("generated/") for rel in walked)
    assert "src/lib.py" in walked


def test_a_saved_nested_keep_still_lifts_an_ignore_file_exclusion(
    saved_nested_keep: Path,
) -> None:
    # Negative: the same keep under a `.gitignore`d directory, with no
    # `--exclude`, is walked as before.
    (saved_nested_keep / ".gitignore").write_text("generated/\n")

    walked = _walked_by_cli(saved_nested_keep, ["start", "--update-graph"])

    assert "generated/node_modules/kept.py" in walked
    assert "generated/out.py" not in walked


def test_a_run_level_file_pattern_still_applies_inside_a_kept_directory(
    saved_nested_keep: Path,
) -> None:
    # Negative: an `--exclude` that encloses nothing keeps the keep, and
    # still wins for the files it names inside it.
    (saved_nested_keep / ".gitignore").write_text("generated/\n")
    (saved_nested_keep / "generated/node_modules/skip.js").write_text("x\n")

    walked = _walked_by_cli(
        saved_nested_keep, ["start", "--update-graph", "--exclude", "*.js"]
    )

    assert "generated/node_modules/kept.py" in walked
    assert "generated/node_modules/skip.js" not in walked
