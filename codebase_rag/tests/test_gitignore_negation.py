"""A `!` line in the repository's `.gitignore` re-includes what git keeps.

`.gitignore` lines were merged into the `.cgrignore` exclude set, where an
explicit exclude always wins and a `!` line only rescues a built-in
exclusion. `src/gen/*.py` + `!src/gen/handwritten.py` therefore dropped the
hand-written file git tracks: no File node, no symbols, `resolve` empty
(issue #2835). Git evaluates the file's lines in order, the last match
winning, and cannot re-include a path whose parent directory is excluded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import (
    CGRIGNORE_FILENAME,
    GITIGNORE_FILENAME,
    load_ignore_patterns,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.main import prompt_for_unignored_directories
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.path_utils import should_skip_path, should_skip_rel_file
from evals.cgr_graph import _StatefulIngestor

_ISSUE_GITIGNORE = "src/gen/*.py\n!src/gen/handwritten.py\n"

# (.gitignore, files): the issue's idioms plus git's two ordering rules.
_CASES = {
    "issue": (
        _ISSUE_GITIGNORE,
        ["src/gen/auto.py", "src/gen/handwritten.py", "src/app.py"],
    ),
    "generated_dir_glob": (
        "generated/*\n!generated/hand_patched.py\n",
        ["generated/auto.py", "generated/hand_patched.py"],
    ),
    "extension": (
        "*.js\n!scripts/build.js\n",
        ["app.js", "scripts/build.js", "lib/scripts/build.js"],
    ),
    "reincluded_directory": (
        "/vendor/*\n!/vendor/our-fork/\n",
        ["vendor/other/a.go", "vendor/our-fork/a.go"],
    ),
    "excluded_parent_wins": (
        "gen/\n!gen/keep.py\n",
        ["gen/keep.py", "gen/auto.py"],
    ),
    "last_match_wins": (
        "*.py\n!keep.py\nkeep.py\n!src/keep.py\n",
        ["keep.py", "src/keep.py", "lib/keep.py", "other.py"],
    ),
}


def _git_ignored(root: Path, rel: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", rel],
        check=False,
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
    )
    return result.returncode == 0


def _repo(tmp_path: Path, gitignore: str, files: list[str]) -> Path:
    root = tmp_path / "gign"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / GITIGNORE_FILENAME).write_text(gitignore, encoding="utf-8")
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    return root


def _cgr_skips(root: Path, rel: str) -> bool:
    patterns = load_ignore_patterns(root)
    return should_skip_path(
        root / rel,
        root,
        exclude_paths=patterns.exclude or None,
        unignore_paths=patterns.unignore or None,
        is_file=True,
    )


@pytest.mark.parametrize("case", sorted(_CASES))
def test_cgr_ignores_exactly_what_git_ignores(tmp_path: Path, case: str) -> None:
    gitignore, files = _CASES[case]
    root = _repo(tmp_path, gitignore, files)
    expected = {rel: _git_ignored(root, rel) for rel in files}
    assert {rel: _cgr_skips(root, rel) for rel in files} == expected


def test_the_rel_file_check_agrees(tmp_path: Path) -> None:
    root = _repo(tmp_path, *_CASES["issue"])
    patterns = load_ignore_patterns(root)
    skipped = {
        rel: should_skip_rel_file(
            rel,
            tuple(rel.split("/")[:-1]),
            exclude_paths=patterns.exclude or None,
            unignore_paths=patterns.unignore or None,
        )
        for rel in ("src/gen/auto.py", "src/gen/handwritten.py")
    }
    assert skipped == {"src/gen/auto.py": True, "src/gen/handwritten.py": False}


def test_the_issue_indexes_the_reincluded_file(tmp_path: Path) -> None:
    root = tmp_path / "gign"
    (root / "src" / "gen").mkdir(parents=True)
    (root / GITIGNORE_FILENAME).write_text(_ISSUE_GITIGNORE, encoding="utf-8")
    (root / "src" / "gen" / "auto.py").write_text(
        "def f_auto():\n    return 1\n", encoding="utf-8"
    )
    (root / "src" / "gen" / "handwritten.py").write_text(
        "def f_handwritten():\n    return 1\n", encoding="utf-8"
    )
    patterns = load_ignore_patterns(root)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="gign",
        exclude_paths=patterns.exclude or None,
        unignore_paths=patterns.unignore or None,
    ).run(force=True)
    functions = {str(qn) for label, qn in store.nodes if label == "Function"}
    assert "gign.src.gen.handwritten.f_handwritten" in functions, sorted(functions)
    assert "gign.src.gen.auto.f_auto" not in functions, sorted(functions)


def test_a_cgrignore_exclude_still_beats_a_gitignore_negation(
    tmp_path: Path,
) -> None:
    # Negative: `.cgrignore` keeps its stricter rule, so its explicit exclude
    # is not undone by a `.gitignore` `!` line.
    root = _repo(tmp_path, *_CASES["issue"])
    (root / CGRIGNORE_FILENAME).write_text("src/gen/\n", encoding="utf-8")
    assert _cgr_skips(root, "src/gen/handwritten.py")


def test_a_cgrignore_negation_cannot_rescue_its_own_exclude(tmp_path: Path) -> None:
    # Negative: `.cgrignore`'s own `!` still rescues only built-in exclusions.
    root = _repo(tmp_path, "", ["gen/keep.py"])
    (root / CGRIGNORE_FILENAME).write_text("gen/*\n!gen/keep.py\n", encoding="utf-8")
    assert _cgr_skips(root, "gen/keep.py")


def test_a_gitignore_without_negations_loads_as_before(tmp_path: Path) -> None:
    # Negative: with no `!` line the exclude set is the file's own lines.
    root = _repo(tmp_path, "results/\n*.gen.py\n", [])
    assert load_ignore_patterns(root).exclude == frozenset({"results/", "*.gen.py"})


def test_interactive_setup_offers_the_exclude_line(tmp_path: Path) -> None:
    # The setup prompt lists each exclude by its pattern; an ordered block is
    # offered by its exclude line, never as a multi-line row.
    root = _repo(tmp_path, *_CASES["issue"])
    with (
        patch("codebase_rag.main.Prompt.ask", return_value="all"),
        patch("codebase_rag.main.app_context"),
    ):
        kept = prompt_for_unignored_directories(root)
    assert "src/gen/*.py" in kept, kept
    assert not any(cs.IGNORE_BLOCK_SEPARATOR in pattern for pattern in kept), kept
