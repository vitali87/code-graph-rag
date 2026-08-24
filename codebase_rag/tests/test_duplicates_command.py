from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import _duplicates_group_cell, _duplicates_location_cell, app
from codebase_rag.config import settings
from codebase_rag.types_defs import (
    DuplicateGroup,
    DuplicateMember,
    PropertyValue,
    ResultRow,
)


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
    *,
    projects: list[str],
    rows: list[ResultRow],
    skipped: int = 0,
    root: str | None = None,
) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = projects

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DUPLICATE_FINGERPRINTS:
            return rows
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: name, cs.KEY_ROOT_PATH: root} for name in projects]
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

    def test_stale_graph_with_zero_fingerprints_recommends_reindex(
        self, runner: CliRunner
    ) -> None:
        # A graph indexed before fingerprint stamping has symbols but zero
        # fingerprints: "No duplicated functions found" plus the pattern-tier
        # notice misdiagnoses it. The command must say the graph predates
        # structural fingerprints and recommend a re-index.
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=[], skipped=30190)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates"])

        assert result.exit_code == 0
        assert "re-index" in result.output.lower()
        assert cs.CLI_DUPLICATES_NONE not in result.output

    def test_some_fingerprints_and_no_groups_still_reports_clean(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        # Symbols WERE analyzed and none duplicated: the clean message stays,
        # with the ordinary skipped notice for the bodiless remainder.
        lone = [dict(clone_rows[0], ast_fingerprint="one-of-a-kind")]
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=lone, skipped=3)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates"])

        assert result.exit_code == 0
        assert cs.CLI_DUPLICATES_NONE in result.output
        assert "re-index" not in result.output.lower()

    def test_unknown_project_errors(self, runner: CliRunner) -> None:
        # `-n typo` must error, not scan a nonexistent prefix and report a
        # clean project.
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=[])
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates", "-n", "typo"])

        assert result.exit_code == 1
        assert "is not indexed" in result.output
        # The user error must be raised AFTER the connection closes cleanly,
        # or the service layer logs a spurious ERROR + traceback on exit.
        assert mock_ingestor.__exit__.call_args[0][0] is None

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


class TestClickableLocations:
    """Location cells become OSC 8 hyperlinks into the editor (issue: the
    report was dead text; a member click must open the file at its line)."""

    @pytest.fixture(autouse=True)
    def _neutral_editor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(cs.ENV_CF_BUNDLE_ID, raising=False)
        monkeypatch.setattr(settings, "CGR_EDITOR", cs.EDITOR_AUTO)
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", None)
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", None)

    def test_location_cell_links_to_editor_when_root_known(self) -> None:
        member = _member(path="b.py", start_line=5, end_line=12)
        cell = _duplicates_location_cell(member, Path("/repo"))
        assert isinstance(cell, Text)
        assert cell.plain == "b.py:5-12"
        assert [span.style for span in cell.spans] == [
            "link vscode://file//repo/b.py:5"
        ]

    def test_location_cell_is_plain_without_root(self) -> None:
        member = _member(path="b.py", start_line=5, end_line=12)
        assert _duplicates_location_cell(member, None) == "b.py:5-12"

    def test_location_cell_is_plain_when_links_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR", cs.EDITOR_NONE)
        member = _member(path="b.py", start_line=5, end_line=12)
        assert _duplicates_location_cell(member, Path("/repo")) == "b.py:5-12"

    def test_group_cell_links_to_side_by_side_pair(self) -> None:
        # Clicking the group number opens both members side by side in a
        # terminal that understands diff:// (Croft).
        group = _group(
            _member(path="b.py", start_line=5, end_line=12),
            _member(path="s.py", start_line=8, end_line=15),
        )
        cell = _duplicates_group_cell(1, group, Path("/repo"))
        assert isinstance(cell, Text)
        assert cell.plain == "1"
        assert [span.style for span in cell.spans] == [
            "link diff://open?left=%2Frepo%2Fb.py%3A5&right=%2Frepo%2Fs.py%3A8"
        ]

    def test_group_cell_is_plain_without_root(self) -> None:
        group = _group(
            _member(path="b.py", start_line=5, end_line=12),
            _member(path="s.py", start_line=8, end_line=15),
        )
        assert _duplicates_group_cell(1, group, None) == "1"


class TestOpenGroup:
    """--open N opens a group's first two members side by side."""

    @pytest.fixture(autouse=True)
    def _diff_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "difftool {left} {right}")

    def test_open_spawns_side_by_side_diff(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], rows=clone_rows, root="/repo"
        )
        with (
            patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor),
            patch("codebase_rag.cli.subprocess.Popen") as popen,
        ):
            result = runner.invoke(app, ["duplicates", "--open", "1"])

        assert result.exit_code == 0
        argv = popen.call_args.args[0]
        # Members are path-sorted, so b.py is left and s.py right.
        assert argv == ["difftool", "/repo/b.py", "/repo/s.py"]

    def test_open_unknown_group_errors(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], rows=clone_rows, root="/repo"
        )
        with (
            patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor),
            patch("codebase_rag.cli.subprocess.Popen") as popen,
        ):
            result = runner.invoke(app, ["duplicates", "--open", "5"])

        assert result.exit_code == 1
        assert "does not exist" in result.output
        popen.assert_not_called()

    def test_open_without_recorded_root_errors(
        self, runner: CliRunner, clone_rows: list[ResultRow]
    ) -> None:
        # Legacy graphs predate Project.root_path; --open must say why it
        # cannot open rather than guessing a path.
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], rows=clone_rows)
        with (
            patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor),
            patch("codebase_rag.cli.subprocess.Popen") as popen,
        ):
            result = runner.invoke(app, ["duplicates", "--open", "1"])

        assert result.exit_code == 1
        assert "records no root path" in result.output
        popen.assert_not_called()

    def test_open_with_malformed_diff_command_errors_cleanly(
        self,
        runner: CliRunner,
        clone_rows: list[ResultRow],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "tool '{left} {right}")
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], rows=clone_rows, root="/repo"
        )
        with (
            patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor),
            patch("codebase_rag.cli.subprocess.Popen") as popen,
        ):
            result = runner.invoke(app, ["duplicates", "--open", "1"])

        assert result.exit_code == 1
        assert "CGR_DIFF_COMMAND" in result.output
        popen.assert_not_called()

    def test_malformed_url_template_degrades_to_plain_with_notice(
        self,
        runner: CliRunner,
        clone_rows: list[ResultRow],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A typo'd CGR_EDITOR_URL_TEMPLATE must not abort the report
        # mid-table; locations render plain and a notice says why.
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", "ed://{unknown}")
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], rows=clone_rows, root="/repo"
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["duplicates"])

        assert result.exit_code == 0
        assert "total_price" in result.output
        assert "CGR_EDITOR_URL_TEMPLATE" in result.output


def _member(*, path: str, start_line: int, end_line: int) -> DuplicateMember:
    return DuplicateMember(
        label="Function",
        name="f",
        qualified_name=f"myproj.{path}.f",
        path=path,
        start_line=start_line,
        end_line=end_line,
    )


def _group(*members: DuplicateMember) -> DuplicateGroup:
    return DuplicateGroup(
        kind=cs.KIND_EXACT,
        similarity=1.0,
        node_count=24,
        members=list(members),
    )
