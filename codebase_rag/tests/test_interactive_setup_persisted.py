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
from codebase_rag.utils.path_utils import should_skip_path, walk_eligible_files


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


def test_a_keep_inside_an_excluded_directory_is_reported_not_kept(
    generated: Path, console: MagicMock
) -> None:
    # A `!` line cannot keep a path inside an excluded directory: as in git,
    # a path whose parent directory is excluded stays excluded (issue #2835).
    # So keeping `generated/node_modules` under a `.gitignore`d `generated/`
    # is reported with the line that wins, and nothing is saved. Rows: bin,
    # generated, node_modules.
    kept, confirm_ask = _keep(generated, "3", confirm=True)

    assert kept >= {"generated/node_modules"}
    confirm_ask.assert_not_called()
    assert not (generated / CGRIGNORE_FILENAME).exists()
    assert "Not kept: generated/node_modules (excluded by generated/)" in _printed(
        console
    )
    walked = _walked(generated)
    assert not any(rel.startswith("generated/") for rel in walked)
    assert "src/lib.py" in walked


def test_a_keep_with_no_excluded_ancestor_behaves_as_before(
    generated: Path, console: MagicMock
) -> None:
    # Negative: keeping `bin` lifts nothing else, and `generated/` stays out.
    _keep(generated, "1", confirm=True)

    walked = _walked(generated)
    assert "bin/cli.py" in walked
    assert not any(rel.startswith("generated/") for rel in walked)


def test_an_excluded_file_inside_a_kept_directory_stays_excluded(
    repo: Path, console: MagicMock
) -> None:
    # Negative: keeping a directory does not lift a pattern naming files
    # inside it. Rows: *.js, bin.
    (repo / ".gitignore").write_text("*.js\n")
    (repo / "bin" / "tool.js").write_text("x\n")

    _keep(repo, "2", confirm=True)

    walked = _walked(repo)
    assert "bin/cli.py" in walked
    assert "bin/tool.js" not in walked


@pytest.fixture
def broadly_excluded(repo: Path) -> Path:
    # `.cgrignore` excludes every `bin/`, so the kept `app/bin` is matched by
    # a line that is not the kept path itself.
    (repo / "app" / "bin").mkdir(parents=True)
    (repo / "app" / "bin" / "cli.py").write_text("def cli():\n    pass\n")
    (repo / CGRIGNORE_FILENAME).write_text("bin/\n")
    return repo


def _keep_nested(
    repo: Path, answers: list[str], confirm: bool
) -> tuple[frozenset[str], MagicMock]:
    with (
        patch("codebase_rag.main.Prompt.ask", side_effect=answers),
        patch("rich.prompt.Confirm.ask", return_value=confirm) as confirm_ask,
    ):
        kept = prompt_for_unignored_directories(repo)
    return kept, confirm_ask


def test_a_keep_another_exclusion_matches_is_reported_not_saved(
    broadly_excluded: Path, console: MagicMock
) -> None:
    # Greptile review of PR 2510: saving `!app/bin` left `bin/` matching it,
    # so this run and every later one pruned it while setup reported it
    # saved. Rows: bin (nested: app/bin, bin, bin/); keep only app/bin.
    before = (broadly_excluded / CGRIGNORE_FILENAME).read_text()

    _, confirm_ask = _keep_nested(broadly_excluded, ["1e", "1"], confirm=True)

    confirm_ask.assert_not_called()
    assert (broadly_excluded / CGRIGNORE_FILENAME).read_text() == before
    printed = _printed(console)
    assert "Not kept: app/bin (excluded by bin/)" in printed
    assert "every later sync keeps them" not in printed
    assert "app/bin/cli.py" not in _walked(broadly_excluded)


def test_a_blocked_keep_does_not_stop_the_others_being_saved(
    broadly_excluded: Path, console: MagicMock
) -> None:
    # Negative: a keep no other line matches is saved as before beside the
    # one reported.
    (broadly_excluded / "app" / "node_modules").mkdir()
    (broadly_excluded / "app" / "node_modules" / "m.py").write_text("X = 1\n")

    # Rows: bin, node_modules. Expand bin and keep app/bin; keep node_modules.
    _keep_nested(broadly_excluded, ["1e,2", "1"], confirm=True)

    lines = (broadly_excluded / CGRIGNORE_FILENAME).read_text().splitlines()
    assert "!app/node_modules" in lines
    assert "!app/bin" not in lines
    assert "bin/" in lines
    walked = _walked(broadly_excluded)
    assert "app/node_modules/m.py" in walked
    assert "app/bin/cli.py" not in walked
    assert "Not kept: app/bin" in _printed(console)


def test_keeping_the_exclusion_itself_still_lifts_it(
    broadly_excluded: Path, console: MagicMock
) -> None:
    # Negative: keeping the whole `bin` group includes the `bin/` line itself,
    # which is lifted, so `app/bin` is no longer blocked and is kept.
    _keep_nested(broadly_excluded, ["1"], confirm=True)

    lines = (broadly_excluded / CGRIGNORE_FILENAME).read_text().splitlines()
    assert "bin/" not in lines
    assert "!app/bin" in lines
    assert "app/bin/cli.py" in _walked(broadly_excluded)
    assert "Not kept" not in _printed(console)


def test_a_saved_keep_inside_an_excluded_directory_keeps_nothing(
    repo: Path, console: MagicMock
) -> None:
    # With `secrets/` excluded, a `!secrets/public` line beside it keeps
    # nothing below it: an exclusion wins over a keep (issue #2835).
    (repo / CGRIGNORE_FILENAME).write_text("secrets/\n!secrets/public\n")
    for rel in ("secrets/public/a.py", "secrets/public/secrets/config.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("X = 1\n")

    walked = _walked(repo)

    assert not any(rel.startswith("secrets/") for rel in walked)
    assert "src/lib.py" in walked


@pytest.fixture
def negated(repo: Path) -> Path:
    # A `.gitignore` with a `!` line: its lines are one ordered block, the
    # last matching line deciding (issue #2835).
    (repo / ".gitignore").write_text(
        "results/\ngenerated/\nsrc/gen/*.py\n!src/gen/handwritten.py\n"
    )
    for rel in (
        "results/run.py",
        "generated/node_modules/m.py",
        "src/gen/auto.py",
        "src/gen/handwritten.py",
    ):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("X = 1\n")
    return repo


def test_a_keep_of_a_line_in_a_gitignore_with_negations_is_saved(
    negated: Path, console: MagicMock
) -> None:
    # The saved `!results/` cancels the block's `results/` line, as it does
    # in a `.gitignore` without `!` lines, but the keep was reported blocked
    # by the whole block. Rows: bin, generated, node_modules, results, src.
    _, confirm_ask = _keep(negated, "4", confirm=True)

    confirm_ask.assert_called_once()
    assert "!results/" in (negated / CGRIGNORE_FILENAME).read_text().splitlines()
    assert "Not kept" not in _printed(console)
    walked = _walked(negated)
    assert "results/run.py" in walked
    # The rest of the block still applies, its `!` line included.
    assert "src/gen/handwritten.py" in walked
    assert "src/gen/auto.py" not in walked


def test_a_keep_a_line_of_the_block_excludes_names_that_line(
    negated: Path, console: MagicMock
) -> None:
    # Negative: a keep inside a directory the block excludes is still not
    # saved, and the message names that line rather than the whole block.
    _, confirm_ask = _keep(negated, "3", confirm=True)

    confirm_ask.assert_not_called()
    assert not (negated / CGRIGNORE_FILENAME).exists()
    assert "Not kept: generated/node_modules (excluded by generated/)" in _printed(
        console
    )


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


def test_an_ignore_file_exclusion_beats_a_saved_nested_keep(
    saved_nested_keep: Path,
) -> None:
    # The same keep under a `.gitignore`d directory keeps nothing either: the
    # directory's exclusion wins (issue #2835).
    (saved_nested_keep / ".gitignore").write_text("generated/\n")

    walked = _walked_by_cli(saved_nested_keep, ["start", "--update-graph"])

    assert not any(rel.startswith("generated/") for rel in walked)
    assert "src/lib.py" in walked


def test_a_run_level_file_pattern_still_applies_inside_a_kept_directory(
    saved_nested_keep: Path,
) -> None:
    # Negative: with no enclosing exclusion the keep rescues the default-
    # excluded `node_modules`, and an `--exclude` naming files inside it
    # still wins for those files.
    (saved_nested_keep / "generated/node_modules/skip.js").write_text("x\n")

    walked = _walked_by_cli(
        saved_nested_keep, ["start", "--update-graph", "--exclude", "*.js"]
    )

    assert "generated/node_modules/kept.py" in walked
    assert "generated/node_modules/skip.js" not in walked
