from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.config import (
    CGRIGNORE_FILENAME,
    EMPTY_CGRIGNORE,
    load_cgrignore_patterns,
)
from codebase_rag.main import prompt_for_unignored_directories
from codebase_rag.types_defs import CgrignorePatterns


def test_returns_empty_when_no_file(temp_repo: Path) -> None:
    result = load_cgrignore_patterns(temp_repo)
    assert result == EMPTY_CGRIGNORE


def test_loads_exclude_patterns_from_file(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text(encoding="utf-8", data="vendor\nmy_build\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result.exclude
    assert "my_build" in result.exclude
    assert len(result.exclude) == 2
    assert len(result.unignore) == 0


def test_ignores_comments_and_blank_lines(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text(
        encoding="utf-8", data="# Comment\n\nvendor\n  # Indented comment\n"
    )

    result = load_cgrignore_patterns(temp_repo)

    assert result.exclude == frozenset({"vendor"})
    assert result.unignore == frozenset()


def test_strips_whitespace(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text(encoding="utf-8", data="  vendor  \n\ttemp\t\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result.exclude
    assert "temp" in result.exclude


def test_returns_empty_on_read_error(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text(encoding="utf-8", data="vendor")

    original_open = Path.open

    def mock_open(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        if self.name == CGRIGNORE_FILENAME:
            raise PermissionError("Cannot read")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mock_open)

    result = load_cgrignore_patterns(temp_repo)
    assert result == EMPTY_CGRIGNORE


def test_handles_duplicates(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text(encoding="utf-8", data="vendor\nvendor\ntemp\n")

    result = load_cgrignore_patterns(temp_repo)

    assert len(result.exclude) == 2


def test_returns_empty_if_cgrignore_is_a_directory(temp_repo: Path) -> None:
    cgrignore_path = temp_repo / CGRIGNORE_FILENAME
    cgrignore_path.mkdir()

    result = load_cgrignore_patterns(temp_repo)

    assert result == EMPTY_CGRIGNORE


class TestNegationSyntax:
    def test_parses_negation_patterns(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="!vendor\n!node_modules\n")

        result = load_cgrignore_patterns(temp_repo)

        assert result.unignore == frozenset({"vendor", "node_modules"})
        assert result.exclude == frozenset()

    def test_mixed_exclude_and_negation(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text(
            encoding="utf-8", data="custom_build\n!vendor\ntemp_data\n!node_modules\n"
        )

        result = load_cgrignore_patterns(temp_repo)

        assert result.exclude == frozenset({"custom_build", "temp_data"})
        assert result.unignore == frozenset({"vendor", "node_modules"})

    def test_negation_strips_leading_whitespace(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="  !vendor  \n")

        result = load_cgrignore_patterns(temp_repo)

        assert result.unignore == frozenset({"vendor"})

    def test_negation_strips_whitespace_after_exclamation(
        self, temp_repo: Path
    ) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="!  foo\n!   bar  \n")

        result = load_cgrignore_patterns(temp_repo)

        assert result.unignore == frozenset({"foo", "bar"})

    def test_returns_cgrignore_patterns_type(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="exclude\n!unignore\n")

        result = load_cgrignore_patterns(temp_repo)

        assert isinstance(result, CgrignorePatterns)


class TestCgrignoreIntegration:
    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_patterns_included_in_candidates(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="vendor\ncustom_cache\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path)

        assert ".git" in result
        assert "vendor" in result
        assert "custom_cache" in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_merged_with_cli_excludes(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="from_cgrignore\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path, cli_excludes=["from_cli"])

        assert "from_cgrignore" in result
        assert "from_cli" in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_only_returns_without_prompt_when_empty(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        result = prompt_for_unignored_directories(tmp_path)

        assert result == frozenset()
        mock_ask.assert_not_called()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_alone_triggers_prompt(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="my_custom_dir\n")
        mock_ask.return_value = "none"

        prompt_for_unignored_directories(tmp_path)

        mock_ask.assert_called_once()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_deduplicates_with_detected(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data=".git\nvendor\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path)

        assert ".git" in result
        assert "vendor" in result
        assert len([x for x in result if x == ".git"]) == 1


class TestNegationIntegration:
    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_unignore_included_when_user_selects_none(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="custom_exclude\n!vendor\n")
        mock_ask.return_value = "none"

        result = prompt_for_unignored_directories(tmp_path)

        assert "vendor" in result
        assert "custom_exclude" not in result
        assert ".git" not in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_unignore_merged_with_user_selection(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="!vendor\n")
        mock_ask.return_value = "1"

        result = prompt_for_unignored_directories(tmp_path)

        assert "vendor" in result
        assert ".git" in result or "node_modules" in result

    def test_unignore_only_returns_without_prompt(self, tmp_path: Path) -> None:
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="!vendor\n!node_modules\n")

        result = prompt_for_unignored_directories(tmp_path)

        assert result == frozenset({"vendor", "node_modules"})

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_unignore_included_when_user_selects_all(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(encoding="utf-8", data="custom\n!vendor\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path)

        assert "vendor" in result
        assert ".git" in result
        assert "custom" in result


class TestCgrignoreLoadedWithoutInteractiveSetup:
    runner = CliRunner()

    @patch("codebase_rag.cli.GraphUpdater")
    @patch("codebase_rag.cli.load_parsers", return_value=({}, {}))
    @patch("codebase_rag.cli.connect_memgraph")
    @patch("codebase_rag.cli.load_cgrignore_patterns")
    def test_start_loads_cgrignore_without_interactive_setup(
        self,
        mock_load_cgrignore: MagicMock,
        mock_connect: MagicMock,
        mock_load_parsers: MagicMock,
        mock_graph_updater: MagicMock,
        tmp_path: Path,
    ) -> None:
        cgrignore_patterns = CgrignorePatterns(
            exclude=frozenset({"vendor", "build"}),
            unignore=frozenset({"vendor/important"}),
        )
        mock_load_cgrignore.return_value = cgrignore_patterns

        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = self.runner.invoke(
            app,
            ["start", "--update-graph", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        mock_load_cgrignore.assert_called_once_with(tmp_path)
        updater_call = mock_graph_updater.call_args
        passed_unignore = updater_call.args[4]
        passed_exclude = updater_call.args[5]
        assert passed_unignore == frozenset({"vendor/important"})
        assert "vendor" in passed_exclude
        assert "build" in passed_exclude

    @patch("codebase_rag.cli.GraphUpdater")
    @patch("codebase_rag.cli.load_parsers", return_value=({}, {}))
    @patch("codebase_rag.cli.ProtobufFileIngestor")
    @patch("codebase_rag.cli.load_cgrignore_patterns")
    def test_index_loads_cgrignore_without_interactive_setup(
        self,
        mock_load_cgrignore: MagicMock,
        mock_proto_ingestor: MagicMock,
        mock_load_parsers: MagicMock,
        mock_graph_updater: MagicMock,
        tmp_path: Path,
    ) -> None:
        cgrignore_patterns = CgrignorePatterns(
            exclude=frozenset({"dist"}),
            unignore=frozenset({"dist/assets"}),
        )
        mock_load_cgrignore.return_value = cgrignore_patterns

        output_dir = str(tmp_path / "output")

        result = self.runner.invoke(
            app,
            ["index", "--repo-path", str(tmp_path), "-o", output_dir],
        )

        assert result.exit_code == 0, result.output
        mock_load_cgrignore.assert_called_once_with(tmp_path)
        updater_call = mock_graph_updater.call_args
        passed_unignore = updater_call.args[4]
        passed_exclude = updater_call.args[5]
        assert passed_unignore == frozenset({"dist/assets"})
        assert "dist" in passed_exclude

    @patch("codebase_rag.cli.GraphUpdater")
    @patch("codebase_rag.cli.load_parsers", return_value=({}, {}))
    @patch("codebase_rag.cli.connect_memgraph")
    @patch("codebase_rag.cli.load_cgrignore_patterns")
    def test_start_merges_cli_excludes_with_cgrignore(
        self,
        mock_load_cgrignore: MagicMock,
        mock_connect: MagicMock,
        mock_load_parsers: MagicMock,
        mock_graph_updater: MagicMock,
        tmp_path: Path,
    ) -> None:
        cgrignore_patterns = CgrignorePatterns(
            exclude=frozenset({"from_cgrignore"}),
            unignore=frozenset(),
        )
        mock_load_cgrignore.return_value = cgrignore_patterns

        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = self.runner.invoke(
            app,
            [
                "start",
                "--update-graph",
                "--repo-path",
                str(tmp_path),
                "--exclude",
                "from_cli",
            ],
        )

        assert result.exit_code == 0, result.output
        updater_call = mock_graph_updater.call_args
        passed_exclude = updater_call.args[5]
        assert "from_cgrignore" in passed_exclude
        assert "from_cli" in passed_exclude

    @patch("codebase_rag.cli.prompt_for_unignored_directories")
    @patch("codebase_rag.cli.GraphUpdater")
    @patch("codebase_rag.cli.load_parsers", return_value=({}, {}))
    @patch("codebase_rag.cli.connect_memgraph")
    @patch("codebase_rag.cli.load_cgrignore_patterns")
    def test_start_does_not_prompt_without_interactive_setup(
        self,
        mock_load_cgrignore: MagicMock,
        mock_connect: MagicMock,
        mock_load_parsers: MagicMock,
        mock_graph_updater: MagicMock,
        mock_prompt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_load_cgrignore.return_value = CgrignorePatterns(
            exclude=frozenset({"vendor"}),
            unignore=frozenset({"vendor/keep"}),
        )

        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = self.runner.invoke(
            app,
            ["start", "--update-graph", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        mock_prompt.assert_not_called()
        mock_load_cgrignore.assert_called_once()
