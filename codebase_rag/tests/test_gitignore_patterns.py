from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.config import (
    CGRIGNORE_FILENAME,
    GITIGNORE_FILENAME,
    load_ignore_patterns,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_gitignore_negation import _git_ignored, _repo
from codebase_rag.utils.path_utils import matches_ignore_patterns, should_skip_rel_file
from evals.cgr_graph import _StatefulIngestor


def test_gitignore_excludes_are_loaded(tmp_path: Path) -> None:
    # A gitignored path is a build artifact or generated output; indexing it
    # pollutes the graph and the dead-code report (evals/results fixtures on
    # cgr's own repo), so root .gitignore patterns must merge into excludes.
    (tmp_path / GITIGNORE_FILENAME).write_text("results/\n*.gen.py\n", encoding="utf-8")

    result = load_ignore_patterns(tmp_path)

    assert "results/" in result.exclude
    assert "*.gen.py" in result.exclude


def test_gitignore_exact_negation_cancels_its_exclude(tmp_path: Path) -> None:
    # An exact-match `!pattern` negation cancels the same-string exclude at
    # load time; the runtime skip check gives excludes precedence over
    # unignores, so leaving both in place would keep the path skipped.
    (tmp_path / GITIGNORE_FILENAME).write_text(
        "dist/\n!dist/\nbuild/\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert "dist/" not in result.exclude
    assert "build/" in result.exclude


def test_gitignore_finer_negation_stays_an_unignore(tmp_path: Path) -> None:
    # A finer-grained negation (`!dist/keep.py` under an excluded `dist/`)
    # cannot re-include the file: git never re-includes a path whose parent
    # directory is excluded. It still flows to unignore, where it rescues
    # from built-in ignores.
    (tmp_path / GITIGNORE_FILENAME).write_text(
        "dist/\n!dist/keep.py\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert matches_ignore_patterns("dist/", result.exclude)
    assert matches_ignore_patterns("dist/keep.py", result.exclude)
    assert "dist/keep.py" in result.unignore


def test_cgrignore_unignore_overrides_gitignore_exclude(tmp_path: Path) -> None:
    # .cgrignore stays authoritative for cgr-specific choices; a `!pattern`
    # there re-includes something .gitignore excludes (the escape hatch for
    # indexing generated code on purpose). The cancellation must happen at
    # load time or the runtime exclude-first precedence keeps it skipped.
    (tmp_path / GITIGNORE_FILENAME).write_text("generated/\n", encoding="utf-8")
    (tmp_path / CGRIGNORE_FILENAME).write_text(
        "vendor_src\n!generated/\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert "generated/" not in result.exclude
    assert "vendor_src" in result.exclude


def test_cgrignore_exclude_wins_over_gitignore_negation(tmp_path: Path) -> None:
    # The override channel is one-directional: an explicit .cgrignore exclude
    # is authoritative and a .gitignore negation must not cancel it.
    (tmp_path / GITIGNORE_FILENAME).write_text("!generated/\n", encoding="utf-8")
    (tmp_path / CGRIGNORE_FILENAME).write_text("generated/\n", encoding="utf-8")

    result = load_ignore_patterns(tmp_path)

    assert "generated/" in result.exclude


def test_no_ignore_files_yields_empty(tmp_path: Path) -> None:
    result = load_ignore_patterns(tmp_path)

    assert result.exclude == frozenset()
    assert result.unignore == frozenset()


def _cgr_skips(root: Path, rel: str) -> bool:
    patterns = load_ignore_patterns(root)
    return should_skip_rel_file(
        rel,
        tuple(rel.split("/")[:-1]),
        exclude_paths=patterns.exclude or None,
        unignore_paths=patterns.unignore or None,
    )


def _indexed_files(root: Path) -> set[str]:
    # The files an actual run puts in the graph, with the patterns loaded the
    # way `cgr start` loads them.
    patterns = load_ignore_patterns(root)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=root.name,
        exclude_paths=patterns.exclude or None,
        unignore_paths=patterns.unignore or None,
    ).run(force=True)
    return {
        str(props[cs.KEY_PATH])
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.FILE
    }


def _load_logging_warnings(root: Path) -> tuple[frozenset[str], list[str]]:
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        return load_ignore_patterns(root).exclude, warnings
    finally:
        logger.remove(sink)


def test_an_escaped_trailing_space_is_kept_as_git_keeps_it(tmp_path: Path) -> None:
    # Issue #3723: git trims trailing spaces unless a backslash escapes them,
    # so `t.py\ ` names the file `t.py ` (with the space) and not `t.py`.
    # `str.strip` removed the escaped space and left `t.py\`, a dangling
    # escape pathspec refuses.
    root = _repo(tmp_path, "t.py\\ \n", [])

    assert _git_ignored(root, "t.py ")
    assert not _git_ignored(root, "t.py")
    assert load_ignore_patterns(root).exclude == frozenset({"t.py\\ "})
    assert _cgr_skips(root, "t.py ")
    assert not _cgr_skips(root, "t.py")


def test_only_an_unescaped_trailing_space_is_trimmed(tmp_path: Path) -> None:
    # Negative: `\\` escapes the backslash, not the space after it, so that
    # space is trimmed; leading whitespace is dropped as before.
    (tmp_path / GITIGNORE_FILENAME).write_text("u\\\\ \n  v.py \n", encoding="utf-8")

    assert load_ignore_patterns(tmp_path).exclude == frozenset({"u\\\\", "v.py"})


def test_an_escaped_trailing_space_does_not_stop_the_run(tmp_path: Path) -> None:
    root = _repo(tmp_path, "t.py\\ \nsecret.py\n", ["t.py", "secret.py"])

    files = _indexed_files(root)

    assert "t.py" in files, sorted(files)
    assert "secret.py" not in files, sorted(files)


def test_a_line_pathspec_refuses_is_skipped_with_a_warning(tmp_path: Path) -> None:
    # A line ending in a lone backslash escapes nothing: git matches nothing
    # with it and pathspec refuses it. Compiled with the other lines it failed
    # the whole run (#3723); it is left out on its own, once, by line number.
    root = _repo(tmp_path, "secret.py\nbad\\\n", ["a.py", "secret.py"])
    gitignore = root / GITIGNORE_FILENAME

    excludes, warnings = _load_logging_warnings(root)

    assert excludes == frozenset({"secret.py"})
    assert len(warnings) == 1, warnings
    assert str(gitignore) in warnings[0], warnings
    assert "line 2" in warnings[0], warnings
    files = _indexed_files(root)
    assert "a.py" in files, sorted(files)
    assert "secret.py" not in files, sorted(files)


@pytest.mark.parametrize("line", ["!!", "! !", "!", "!   "])
def test_a_negation_indexing_cannot_compile_is_skipped(
    tmp_path: Path, line: str
) -> None:
    # `!!` and `! !` pass pathspec as lines, but each leaves a bare `!` in the
    # unignore set once its first `!` is dropped, which pathspec refuses when
    # indexing compiles that set. A `!` with nothing after it is refused as a
    # line. Each is skipped with one warning, and the run goes on.
    root = _repo(tmp_path, f"secret.py\n{line}\n", ["a.py", "secret.py"])
    gitignore = root / GITIGNORE_FILENAME

    excludes, warnings = _load_logging_warnings(root)

    assert excludes == frozenset({"secret.py"})
    assert len(warnings) == 1, warnings
    assert str(gitignore) in warnings[0], warnings
    assert "line 2" in warnings[0], warnings
    assert load_ignore_patterns(root).unignore == frozenset()
    files = _indexed_files(root)
    assert "a.py" in files, sorted(files)
    assert "secret.py" not in files, sorted(files)


@pytest.mark.parametrize("line", ["[z-a]", "src/[9-0].py"])
def test_a_line_whose_range_runs_backwards_is_skipped(
    tmp_path: Path, line: str
) -> None:
    # pathspec hands a bracket range such as `[z-a]` to `re.compile`
    # unchanged, which raises `re.error`: not a ValueError, so it escaped the
    # per-line check and stopped the run. It is skipped with one warning.
    root = _repo(tmp_path, f"secret.py\n{line}\n", ["a.py", "secret.py"])
    gitignore = root / GITIGNORE_FILENAME

    excludes, warnings = _load_logging_warnings(root)

    assert excludes == frozenset({"secret.py"})
    assert len(warnings) == 1, warnings
    assert str(gitignore) in warnings[0], warnings
    assert "line 2" in warnings[0], warnings
    files = _indexed_files(root)
    assert "a.py" in files, sorted(files)
    assert "secret.py" not in files, sorted(files)


def test_a_byte_order_mark_is_not_part_of_the_first_pattern(tmp_path: Path) -> None:
    # Issue #3724: an editor that saves a UTF-8 byte-order mark put it in
    # front of the first pattern, which then matched nothing. Git skips it.
    root = _repo(tmp_path, "\ufeffsecret.py\nbuild.py\n", [])

    assert _git_ignored(root, "secret.py")
    assert load_ignore_patterns(root).exclude == frozenset({"secret.py", "build.py"})


def test_a_byte_order_mark_does_not_let_the_first_file_in(tmp_path: Path) -> None:
    root = _repo(tmp_path, "\ufeffsecret.py\n", ["a.py", "secret.py"])

    files = _indexed_files(root)

    assert "a.py" in files, sorted(files)
    assert "secret.py" not in files, sorted(files)
