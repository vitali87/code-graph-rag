from __future__ import annotations

from pathlib import Path

from codebase_rag.config import (
    CGRIGNORE_FILENAME,
    GITIGNORE_FILENAME,
    load_ignore_patterns,
)


def test_gitignore_excludes_are_loaded(tmp_path: Path) -> None:
    # (H) A gitignored path is a build artifact or generated output; indexing it
    # (H) pollutes the graph and the dead-code report (evals/results fixtures on
    # (H) cgr's own repo), so root .gitignore patterns must merge into excludes.
    (tmp_path / GITIGNORE_FILENAME).write_text("results/\n*.gen.py\n", encoding="utf-8")

    result = load_ignore_patterns(tmp_path)

    assert "results/" in result.exclude
    assert "*.gen.py" in result.exclude


def test_gitignore_negations_are_unignores(tmp_path: Path) -> None:
    (tmp_path / GITIGNORE_FILENAME).write_text(
        "dist/\n!dist/keep.py\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert "dist/" in result.exclude
    assert "dist/keep.py" in result.unignore


def test_cgrignore_and_gitignore_merge(tmp_path: Path) -> None:
    # (H) .cgrignore stays authoritative for cgr-specific choices; a `!pattern`
    # (H) there re-includes something .gitignore excludes (the escape hatch for
    # (H) indexing generated code on purpose).
    (tmp_path / GITIGNORE_FILENAME).write_text("generated/\n", encoding="utf-8")
    (tmp_path / CGRIGNORE_FILENAME).write_text(
        "vendor_src\n!generated/\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert "generated/" in result.exclude
    assert "vendor_src" in result.exclude
    assert "generated/" in result.unignore


def test_no_ignore_files_yields_empty(tmp_path: Path) -> None:
    result = load_ignore_patterns(tmp_path)

    assert result.exclude == frozenset()
    assert result.unignore == frozenset()
