from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.types_defs import PropertyValue, ResultRow


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def clone_rows() -> list[ResultRow]:
    return [
        {
            "label": "Function",
            "name": "total_price",
            "qualified_name": "myproj.b.total_price",
            "path": "b.py",
            "start_line": 5,
            "end_line": 12,
            "ast_fingerprint": "f3a9c2d1e4b58607",
            "ast_fingerprint_nodes": 24,
            "ast_branch_fingerprints": ["b1", "b2", "b3"],
        },
        {
            "label": "Function",
            "name": "sum_weights",
            "qualified_name": "myproj.s.sum_weights",
            "path": "s.py",
            "start_line": 8,
            "end_line": 15,
            "ast_fingerprint": "f3a9c2d1e4b58607",
            "ast_fingerprint_nodes": 24,
            "ast_branch_fingerprints": ["b1", "b2", "b3"],
        },
    ]


def _make_mock_ingestor(
    *, projects: list[str], rows: list[ResultRow], skipped: int = 0
) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = projects

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DUPLICATE_FINGERPRINTS:
            return rows
        return [{cs.KEY_SKIPPED: skipped}]

    mock.fetch_all.side_effect = _fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestDuplicatesCommand:
    def test_lists_clone_group_in_table(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=clone_rows)
        # Fixture identifiers are short enough to render un-elided in the
        # five-column table at the CliRunner's default 80 columns (COLUMNS
        # overrides do not reach consoles created at import time on every
        # platform).
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates"])

        assert result.exit_code == 0
        assert "total_price" in result.output
        assert "sum_weights" in result.output

    def test_json_format_emits_envelope_with_coverage(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], rows=clone_rows, skipped=3
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        # Envelope, not a bare list: JSON consumers must see the coverage
        # count or an incomplete scan reads as a complete one.
        assert payload[cs.KEY_SKIPPED_SYMBOLS] == 3
        assert payload[cs.KEY_TRUNCATED] is False
        groups = payload[cs.KEY_DUPLICATE_GROUPS]
        assert len(groups) == 1
        assert groups[0]["kind"] == cs.KIND_EXACT
        assert {m["qualified_name"] for m in groups[0]["members"]} == {
            "myproj.b.total_price",
            "myproj.s.sum_weights",
        }

    def test_fail_on_found_exits_nonzero(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=clone_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates", "--fail-on-found"])

        assert result.exit_code == 1

    def test_clean_project_reports_none(self, runner: CliRunner) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=[])
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates"])

        assert result.exit_code == 0
        assert cs.CLI_DUPLICATES_NONE in result.output

    def test_no_projects_errors(self, runner: CliRunner) -> None:
        mock_ingestor = _make_mock_ingestor(projects=[], rows=[])
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates"])

        assert result.exit_code == 1

    def test_min_size_filters_small_functions(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=clone_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates", "--min-size", "25"])

        assert result.exit_code == 0
        assert cs.CLI_DUPLICATES_NONE in result.output

    def test_skipped_symbols_notice_is_printed(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], rows=clone_rows, skipped=7
        )
        # Rich's highlighter wraps digits and parens in their own ANSI
        # spans, so assert on an unstyled fragment of the notice.
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates"])

        assert result.exit_code == 0
        assert "were not analyzed" in result.output
