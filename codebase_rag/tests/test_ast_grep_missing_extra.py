# Issue #2634: every documented install command left out the `ast-grep`
# extra, so Ruby, Kotlin, Swift, ... files were indexed as bare File nodes
# while the sync reported success. The only hint was one stdlib-logging line
# printed when the updater was BUILT, before any file was seen: it named no
# file or language, it also printed for repositories with no such file, and
# `-q` could not silence it. Installing the extra afterwards did not help
# either, since the next sync of an otherwise unchanged repository took the
# in-sync fast path and never parsed those files.
from __future__ import annotations

import logging
import sys
from collections.abc import Generator
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_fingerprint import compute_parser_fingerprint
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import ast_grep_tier

RUBY = (
    'require "json"\n'
    "\n"
    "class Greeter\n"
    "  def hello(name)\n"
    '    "hi #{name}"\n'
    "  end\n"
    "end\n"
    "\n"
    "def main\n"
    '  Greeter.new.hello("x")\n'
    "end\n"
)
KOTLIN = "package demo\n\nclass Util {\n    fun twice(x: Int): Int = x * 2\n}\n"
PYTHON = "def helper():\n    return 1\n"

_TIER_LOGGERS = (
    "codebase_rag.parsers.ast_grep_tier",
    "codebase_rag.analyzers.ast_grep_analyzer",
)


@pytest.fixture
def warnings() -> Generator[list[str], None, None]:
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]), level="WARNING"
    )
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def without_ast_grep(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry makes `import ast_grep_py` raise ImportError, which is what
    # an install without the extra sees.
    monkeypatch.setitem(sys.modules, "ast_grep_py", None)


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        (repo / rel).write_text(content, encoding="utf-8")


def _sync(repo: Path) -> tuple[GraphUpdater, MagicMock]:
    parsers, queries = load_parsers()
    ingestor = MagicMock()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="demo",
    )
    updater.run()
    return updater, ingestor


def _names(ingestor: MagicMock, label: cs.NodeLabel) -> set[str]:
    return {
        c.args[1].get(cs.KEY_NAME)
        for c in ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == label.value
    }


def _ast_grep_mentions(messages: list[str]) -> list[str]:
    return [m for m in messages if "ast-grep" in m]


def _stdlib_tier_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name in _TIER_LOGGERS]


class TestUnparsedFilesAreNamed:
    def test_one_warning_counts_the_files_and_names_languages_and_extra(
        self, tmp_path: Path, without_ast_grep: None, warnings: list[str]
    ) -> None:
        _write(
            tmp_path,
            {"app.rb": RUBY, "other.rb": RUBY, "util.kt": KOTLIN, "tool.py": PYTHON},
        )

        _sync(tmp_path)

        [message] = _ast_grep_mentions(warnings)
        assert message.startswith("3 file(s) ")
        assert "kotlin: 1" in message
        assert "ruby: 2" in message
        assert "code-graph-rag[" in message and "ast-grep]" in message

    def test_repo_without_tier_files_gets_no_ast_grep_message(
        self,
        tmp_path: Path,
        without_ast_grep: None,
        warnings: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write(tmp_path, {"tool.py": PYTHON})

        with caplog.at_level(logging.DEBUG):
            _sync(tmp_path)

        assert _ast_grep_mentions(warnings) == []
        assert _stdlib_tier_records(caplog) == []

    def test_a_reused_updater_counts_each_run_on_its_own(
        self, tmp_path: Path, without_ast_grep: None, warnings: list[str]
    ) -> None:
        _write(tmp_path, {"app.rb": RUBY})
        updater, _ = _sync(tmp_path)
        (tmp_path / "util.kt").write_text(KOTLIN, encoding="utf-8")
        warnings.clear()

        updater.run(force=True)

        [message] = _ast_grep_mentions(warnings)
        assert message.startswith("2 file(s) ")
        assert "ruby: 1" in message and "kotlin: 1" in message

    def test_files_no_tier_would_parse_are_not_counted(
        self, tmp_path: Path, without_ast_grep: None, warnings: list[str]
    ) -> None:
        _write(
            tmp_path,
            {"app.rb": RUBY, "README.md": "# demo\n", "notes.txt": "plain\n"},
        )

        _sync(tmp_path)

        [message] = _ast_grep_mentions(warnings)
        assert message.startswith("1 file(s) ")
        assert "(ruby: 1)" in message

    def test_a_watched_edit_to_a_tier_file_is_named_too(
        self, tmp_path: Path, without_ast_grep: None, warnings: list[str]
    ) -> None:
        _write(tmp_path, {"app.rb": RUBY, "tool.py": PYTHON})
        updater, _ = _sync(tmp_path)
        (tmp_path / "app.rb").write_text(RUBY + "\ndef extra\nend\n", "utf-8")
        warnings.clear()

        updater.reingest([tmp_path / "app.rb"])

        [message] = _ast_grep_mentions(warnings)
        assert message.startswith("1 file(s) ")
        assert "(ruby: 1)" in message

    def test_a_watched_edit_to_a_python_file_names_nothing(
        self, tmp_path: Path, without_ast_grep: None, warnings: list[str]
    ) -> None:
        _write(tmp_path, {"app.rb": RUBY, "tool.py": PYTHON})
        updater, _ = _sync(tmp_path)
        (tmp_path / "tool.py").write_text(PYTHON + "\nX = 1\n", "utf-8")
        warnings.clear()

        updater.reingest([tmp_path / "tool.py"])

        assert _ast_grep_mentions(warnings) == []


class TestTierLogsThroughLoguru:
    def test_a_broken_pattern_config_is_reported_through_loguru(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        warnings: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def broken() -> NoReturn:
            raise ValueError("ruby.yaml: 'extensions' and 'ast_grep_id' are required")

        monkeypatch.setattr(ast_grep_tier, "load_pattern_configs", broken)

        with caplog.at_level(logging.DEBUG):
            ast_grep_tier.AstGrepTier(MagicMock(), tmp_path, "demo")

        assert any("ruby.yaml" in m for m in _ast_grep_mentions(warnings))
        assert _stdlib_tier_records(caplog) == []

    def test_findings_without_the_extra_are_reported_through_loguru(
        self,
        tmp_path: Path,
        without_ast_grep: None,
        warnings: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from codebase_rag.analyzers import FindingAnalyzer

        with caplog.at_level(logging.DEBUG):
            FindingAnalyzer(MagicMock(), tmp_path, resolve_capture(["+findings"]))

        [message] = _ast_grep_mentions(warnings)
        assert "ast-grep]" in message
        assert _stdlib_tier_records(caplog) == []


class TestInstallingTheExtraReparses:
    def test_unchanged_tier_files_are_parsed_once_the_extra_is_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, {"app.rb": RUBY, "tool.py": PYTHON})
        with monkeypatch.context() as missing:
            missing.setitem(sys.modules, "ast_grep_py", None)
            _sync(tmp_path)
            settled, ingestor = _sync(tmp_path)
        assert settled.skipped_because_in_sync is True
        assert "Greeter" not in _names(ingestor, cs.NodeLabel.CLASS)

        installed, ingestor = _sync(tmp_path)

        assert installed.skipped_because_in_sync is False
        assert "Greeter" in _names(ingestor, cs.NodeLabel.CLASS)
        assert "main" in _names(ingestor, cs.NodeLabel.FUNCTION)

    def test_fingerprint_tracks_whether_ast_grep_imports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        available = compute_parser_fingerprint()
        with monkeypatch.context() as missing:
            missing.setitem(sys.modules, "ast_grep_py", None)
            absent = compute_parser_fingerprint()

        assert absent != available


class TestUnchangedBehaviour:
    def test_with_the_extra_tier_files_parse_and_nothing_is_reported(
        self, tmp_path: Path, warnings: list[str]
    ) -> None:
        _write(tmp_path, {"app.rb": RUBY, "util.kt": KOTLIN})

        _, ingestor = _sync(tmp_path)

        assert _ast_grep_mentions(warnings) == []
        assert {"Greeter", "Util"} <= _names(ingestor, cs.NodeLabel.CLASS)

    def test_without_the_extra_tier_files_still_get_their_file_node(
        self, tmp_path: Path, without_ast_grep: None
    ) -> None:
        _write(tmp_path, {"app.rb": RUBY})

        _, ingestor = _sync(tmp_path)

        assert "app.rb" in _names(ingestor, cs.NodeLabel.FILE)
        assert _names(ingestor, cs.NodeLabel.CLASS) == set()
        assert _names(ingestor, cs.NodeLabel.FUNCTION) == set()

    def test_fingerprint_is_stable_while_ast_grep_availability_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert compute_parser_fingerprint() == compute_parser_fingerprint()
        with monkeypatch.context() as missing:
            missing.setitem(sys.modules, "ast_grep_py", None)
            assert compute_parser_fingerprint() == compute_parser_fingerprint()

    def test_a_missing_extra_still_settles_into_the_in_sync_fast_path(
        self, tmp_path: Path, without_ast_grep: None
    ) -> None:
        _write(tmp_path, {"app.rb": RUBY, "tool.py": PYTHON})
        _sync(tmp_path)
        _sync(tmp_path)

        again, _ = _sync(tmp_path)

        assert again.skipped_because_in_sync is True

    def test_findings_off_stays_silent_without_the_extra(
        self, tmp_path: Path, without_ast_grep: None, warnings: list[str]
    ) -> None:
        from codebase_rag.analyzers import FindingAnalyzer

        FindingAnalyzer(MagicMock(), tmp_path, resolve_capture([]))

        assert _ast_grep_mentions(warnings) == []
