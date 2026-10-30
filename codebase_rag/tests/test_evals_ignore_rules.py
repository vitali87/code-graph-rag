"""`evals.ignore_rules` hands every eval arm the ignore rules the indexer
applies to the same target, so grading covers the file set indexing covers
(issue #1520). Pinned: "nothing configured" is `None` on both sides, the
merge of `.cgrignore`, `.gitignore` and tracked-source rescues is the
indexer's own, and the pair equals what a sync of that target is given."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codebase_rag.config import project_ignore_sets
from codebase_rag.tests.conftest import git_env
from evals.ignore_rules import ignore_rules


def test_a_target_with_no_rules_answers_none_not_empty(tmp_path: Path) -> None:
    # `None` is the indexer's own "no rules"; an empty set is not equivalent
    # everywhere downstream.
    assert ignore_rules(tmp_path) == (None, None)


def test_a_negation_alone_leaves_the_excludes_none(tmp_path: Path) -> None:
    (tmp_path / ".cgrignore").write_text("!vendor/keep\n", encoding="utf-8")
    assert ignore_rules(tmp_path) == (None, frozenset({"vendor/keep"}))


def test_cgrignore_and_gitignore_merge_as_the_indexer_merges_them(
    tmp_path: Path,
) -> None:
    (tmp_path / ".cgrignore").write_text(
        "# local\nbuild/\n\n!vendor/keep\n", encoding="utf-8"
    )
    (tmp_path / ".gitignore").write_text(
        "dist/\n*.log\nbuild/\n!dist/\n!build/\n", encoding="utf-8"
    )

    exclude, unignore = ignore_rules(tmp_path)

    # A negation cancels the .gitignore line it repeats, never a .cgrignore
    # one, so `build/` stays excluded and `dist/` does not.
    assert exclude == frozenset({"build/", "*.log"})
    assert unignore == frozenset({"vendor/keep", "dist/", "build/"})


def test_a_tracked_source_dir_is_rescued_file_by_file(git_repo: Path) -> None:
    (git_repo / "bin").mkdir()
    (git_repo / "bin" / "main.dart").write_text("void main() {}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "bin/main.dart"], cwd=git_repo, check=True, env=git_env()
    )

    exclude, unignore = ignore_rules(git_repo)

    assert exclude is None
    assert unignore == frozenset({"/bin/main.dart"})


@pytest.mark.parametrize(
    ("cgrignore", "gitignore"),
    [("", ""), ("generated/\n!generated/keep.py\n", "out/\n*.tmp\n")],
)
def test_the_rules_are_the_ones_a_sync_of_the_target_applies(
    tmp_path: Path, cgrignore: str, gitignore: str
) -> None:
    (tmp_path / ".cgrignore").write_text(cgrignore, encoding="utf-8")
    (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    assert ignore_rules(tmp_path) == project_ignore_sets(tmp_path)
