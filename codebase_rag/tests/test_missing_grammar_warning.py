"""A sync says when a supported language's grammar is not installed.

The base package ships only `tree-sitter-python`; every other grammar comes
from the `treesitter-full` extra, and three of the docs' getting-started
paths installed without it. A TypeScript repo then synced to bare File nodes
with exit 0 and no word about TypeScript, and `cgr doctor` passed
(issue #2905).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import cli as cli_module
from codebase_rag import constants as cs
from codebase_rag import parser_loader
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.schemas import HealthCheckResult
from codebase_rag.tools.health_checker import HealthChecker

_DOCS = Path(__file__).resolve().parents[2] / "docs"


def _sync(root: Path, languages: set[cs.SupportedLanguage] | None) -> list[str]:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python grammar unavailable")
    if languages is not None:
        parsers = {lang: parsers[lang] for lang in languages}
        queries = {lang: queries[lang] for lang in languages}
    warnings: list[str] = []
    handler = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        GraphUpdater(
            ingestor=MagicMock(), repo_path=root, parsers=parsers, queries=queries
        ).run(force=True)
    finally:
        logger.remove(handler)
    return warnings


def _write_repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "users.ts").write_text(
        "export function findUser(id: number): number {\n  return id;\n}\n"
    )
    (root / "src" / "orders.ts").write_text("export const n = 1;\n")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n")
    (root / "app.py").write_text("def main():\n    return 1\n")
    (root / "notes.txt").write_text("not code\n")
    return root


def _grammar_warnings(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "treesitter-full" in w]


def test_a_sync_names_the_files_whose_grammar_is_missing(tmp_path: Path) -> None:
    # The base install: Python alone.
    warnings = _sync(_write_repo(tmp_path / "repo"), {cs.SupportedLanguage.PYTHON})
    grammar = _grammar_warnings(warnings)
    assert len(grammar) == 1, warnings
    assert "3 file(s)" in grammar[0], grammar
    assert "typescript: 2" in grammar[0], grammar
    assert "go: 1" in grammar[0], grammar


def test_a_sync_with_every_grammar_warns_nothing(tmp_path: Path) -> None:
    # Negative: a full install parses these files, so nothing to report.
    warnings = _sync(_write_repo(tmp_path / "repo"), None)
    assert _grammar_warnings(warnings) == [], warnings


def test_files_no_grammar_covers_are_not_counted(tmp_path: Path) -> None:
    # Negative: a `.txt` or `.md` file has no tree-sitter language at all.
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def main():\n    return 1\n")
    (root / "notes.txt").write_text("not code\n")
    (root / "README.md").write_text("# readme\n")
    warnings = _sync(root, {cs.SupportedLanguage.PYTHON})
    assert _grammar_warnings(warnings) == [], warnings


def test_doctor_names_the_missing_grammars(monkeypatch: pytest.MonkeyPatch) -> None:
    real = parser_loader._get_language_library
    absent = {cs.SupportedLanguage.TS, cs.SupportedLanguage.GO}
    monkeypatch.setattr(
        parser_loader,
        "_get_language_library",
        lambda lang: None if lang in absent else real(lang),
    )
    result = HealthChecker().check_tree_sitter_grammars()
    assert not result.passed, result
    assert result.error is not None
    assert "go" in result.error and "typescript" in result.error, result.error
    assert "treesitter-full" in result.error, result.error


class _GrammarOnlyChecker:
    """Doctor with the grammar check alone, two grammars missing."""

    def run_all_checks(self) -> list[HealthCheckResult]:
        return [self.result]

    def get_summary(self) -> tuple[int, int]:
        return 0, 1

    result = HealthCheckResult(
        name=cs.HEALTH_CHECK_GRAMMARS_MISSING.format(missing=2, count=15),
        passed=False,
        message=cs.HEALTH_CHECK_GRAMMARS_MISSING_MSG,
        error=cs.HEALTH_CHECK_GRAMMARS_MISSING_ERROR.format(languages="go, typescript"),
    )


def test_doctor_prints_the_install_command_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rich read `[treesitter-full]` as a markup tag and dropped it, leaving
    # `pip install "code-graph-rag"`, which installs nothing more.
    monkeypatch.setattr(cli_module, "HealthChecker", _GrammarOnlyChecker)
    result = CliRunner().invoke(cli_module.app, ["doctor"])
    assert 'pip install "code-graph-rag[treesitter-full]"' in result.output, (
        result.output
    )


def test_doctor_passes_with_every_grammar_installed() -> None:
    # Negative: this environment installs the full extra.
    result = HealthChecker().check_tree_sitter_grammars()
    assert result.passed, result


@pytest.mark.parametrize(
    "doc",
    ["index.md", "guide/mcp-server.md", "claude-code-setup.md"],
)
def test_getting_started_installs_include_the_grammars(doc: str) -> None:
    # A bare `pip install code-graph-rag` or `uv sync` installs Python's
    # grammar alone.
    text = (_DOCS / doc).read_text(encoding="utf-8")
    bare = re.findall(r"^\s*(?:pip install code-graph-rag|uv sync)\s*$", text, re.M)
    assert bare == [], bare
