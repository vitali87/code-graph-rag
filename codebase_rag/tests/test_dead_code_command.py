from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.types_defs import ResultRow


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def dead_rows() -> list[ResultRow]:
    return [
        {
            "label": "Function",
            "name": "orphan_one",
            "qualified_name": "myproj.mod.orphan_one",
            "start_line": 5,
            "end_line": 9,
        },
        {
            "label": "Method",
            "name": "orphan_two",
            "qualified_name": "myproj.mod.Thing.orphan_two",
            "start_line": 20,
            "end_line": 25,
        },
    ]


def _make_mock_ingestor(
    *, projects: list[str], fetch_result: list[ResultRow]
) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = projects
    mock.fetch_all.return_value = fetch_result
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestDeadCodeCommand:
    def test_lists_orphans_in_table(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code"])

        assert result.exit_code == 0
        assert "orphan_one" in result.output
        assert "orphan_two" in result.output

    def test_json_format_emits_qualified_names(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        names = {row["qualified_name"] for row in payload}
        assert names == {
            "myproj.mod.orphan_one",
            "myproj.mod.Thing.orphan_two",
        }

    def test_exclude_glob_drops_matching_paths(self, runner: CliRunner) -> None:
        # (H) --exclude drops candidates whose file path matches the glob (generated
        # (H) code) while keeping real orphans elsewhere.
        rows: list[ResultRow] = [
            {
                "label": "Function",
                "name": "gen_helper",
                "qualified_name": "myproj.client.core.gen_helper",
                "path": "client/core/request.ts",
                "start_line": 1,
                "end_line": 3,
            },
            {
                "label": "Function",
                "name": "orphan_one",
                "qualified_name": "myproj.mod.orphan_one",
                "path": "mod.ts",
                "start_line": 5,
                "end_line": 9,
            },
        ]
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(
                app, ["dead-code", "--format", "json", "--exclude", "*client/core*"]
            )

        assert result.exit_code == 0
        names = {row["qualified_name"] for row in json.loads(result.output)}
        assert names == {"myproj.mod.orphan_one"}

    def test_fail_on_found_exits_one_when_dead_code(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--fail-on-found"])

        assert result.exit_code == 1

    def test_fail_on_found_exits_zero_when_clean(self, runner: CliRunner) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=[])
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--fail-on-found"])

        assert result.exit_code == 0

    def test_explicit_project_name_used(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj", "other"], fetch_result=dead_rows
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--project-name", "myproj"])

        assert result.exit_code == 0
        _query, params = mock_ingestor.fetch_all.call_args.args
        assert params["project_prefix"] == "myproj."

    def test_errors_when_project_ambiguous(self, runner: CliRunner) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["a", "b"], fetch_result=[])
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code"])

        assert result.exit_code == 1
        mock_ingestor.fetch_all.assert_not_called()

    def test_errors_when_no_projects(self, runner: CliRunner) -> None:
        mock_ingestor = _make_mock_ingestor(projects=[], fetch_result=[])
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code"])

        assert result.exit_code == 1

    def test_entry_point_forwarded_to_query(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "-e", "main", "-e", "run"])

        assert result.exit_code == 0
        _query, params = mock_ingestor.fetch_all.call_args.args
        assert params["entry_points"] == ["main", "run"]

    def test_decorator_root_extends_defaults(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--decorator-root", "myhandler"])

        assert result.exit_code == 0
        _query, params = mock_ingestor.fetch_all.call_args.args
        assert "myhandler" in params["root_decorators"]
        assert "task" in params["root_decorators"]

    def test_writes_json_to_output_file(
        self, runner: CliRunner, dead_rows: list[ResultRow], tmp_path: Path
    ) -> None:
        out = tmp_path / "dead.json"
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(
                app,
                ["dead-code", "--format", "json", "--output", str(out)],
            )

        assert result.exit_code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert len(payload) == 2

    def test_writes_table_to_output_file(
        self, runner: CliRunner, dead_rows: list[ResultRow], tmp_path: Path
    ) -> None:
        out = tmp_path / "dead.txt"
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--output", str(out)])

        assert result.exit_code == 0
        written = out.read_text(encoding="utf-8")
        assert "orphan_one" in written

    def test_handles_connection_error(self, runner: CliRunner) -> None:
        with patch(
            "codebase_rag.cli.connect_memgraph",
            side_effect=ConnectionError("Cannot connect"),
        ):
            result = runner.invoke(app, ["dead-code"])

        assert result.exit_code == 1

    def test_include_tests_default_passes_test_patterns(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code"])

        assert result.exit_code == 0
        query, params = mock_ingestor.fetch_all.call_args.args
        assert "test_patterns" in params
        assert "$test_patterns" in query

    def test_no_include_tests_omits_test_patterns(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--no-include-tests"])

        assert result.exit_code == 0
        query, params = mock_ingestor.fetch_all.call_args.args
        # (H) test_patterns is still passed (it filters test modules out of the
        # (H) module-load roots and test-file symbols out of the report), but
        # (H) test functions themselves are not roots.
        assert "test_patterns" in params
        assert "OR ANY(p IN $test_patterns WHERE n.path CONTAINS p)" not in query
        assert (
            "AND NOT ANY(p IN $test_patterns WHERE coalesce(n.path, '') CONTAINS p)"
            in query
        )

    def test_classes_flag_includes_class_candidates(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--classes"])

        assert result.exit_code == 0
        query, _params = mock_ingestor.fetch_all.call_args.args
        assert "Function|Method|Class" in query

    def test_classes_off_by_default(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code"])

        assert result.exit_code == 0
        query, _params = mock_ingestor.fetch_all.call_args.args
        assert "Function|Method|Class" not in query
