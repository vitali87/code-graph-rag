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


def test_gitignore_exact_negation_cancels_its_exclude(tmp_path: Path) -> None:
    # (H) An exact-match `!pattern` negation cancels the same-string exclude at
    # (H) load time; the runtime skip check gives excludes precedence over
    # (H) unignores, so leaving both in place would keep the path skipped.
    (tmp_path / GITIGNORE_FILENAME).write_text(
        "dist/\n!dist/\nbuild/\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert "dist/" not in result.exclude
    assert "build/" in result.exclude


def test_gitignore_finer_negation_stays_an_unignore(tmp_path: Path) -> None:
    # (H) A finer-grained negation (`!dist/keep.py` under an excluded `dist/`)
    # (H) cannot cancel by string match; it flows to unignore, where it rescues
    # (H) from built-in ignores only (documented ceiling in config.py).
    (tmp_path / GITIGNORE_FILENAME).write_text(
        "dist/\n!dist/keep.py\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert "dist/" in result.exclude
    assert "dist/keep.py" in result.unignore


def test_cgrignore_unignore_overrides_gitignore_exclude(tmp_path: Path) -> None:
    # (H) .cgrignore stays authoritative for cgr-specific choices; a `!pattern`
    # (H) there re-includes something .gitignore excludes (the escape hatch for
    # (H) indexing generated code on purpose). The cancellation must happen at
    # (H) load time or the runtime exclude-first precedence keeps it skipped.
    (tmp_path / GITIGNORE_FILENAME).write_text("generated/\n", encoding="utf-8")
    (tmp_path / CGRIGNORE_FILENAME).write_text(
        "vendor_src\n!generated/\n", encoding="utf-8"
    )

    result = load_ignore_patterns(tmp_path)

    assert "generated/" not in result.exclude
    assert "vendor_src" in result.exclude


def test_cgrignore_exclude_wins_over_gitignore_negation(tmp_path: Path) -> None:
    # (H) The override channel is one-directional: an explicit .cgrignore exclude
    # (H) is authoritative and a .gitignore negation must not cancel it.
    (tmp_path / GITIGNORE_FILENAME).write_text("!generated/\n", encoding="utf-8")
    (tmp_path / CGRIGNORE_FILENAME).write_text("generated/\n", encoding="utf-8")

    result = load_ignore_patterns(tmp_path)

    assert "generated/" in result.exclude


def test_no_ignore_files_yields_empty(tmp_path: Path) -> None:
    result = load_ignore_patterns(tmp_path)

    assert result.exclude == frozenset()
    assert result.unignore == frozenset()
