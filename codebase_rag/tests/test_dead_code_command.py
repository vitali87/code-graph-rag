from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.types_defs import PropertyValue, ResultRow


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def dead_rows() -> list[ResultRow]:
    # (H) Node rows as the dead-code node fetch returns them; neither symbol has
    # (H) an incoming edge, so both are reported dead.
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
    *,
    projects: list[str],
    fetch_result: list[ResultRow],
    rels: list[ResultRow] | None = None,
) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = projects

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return fetch_result
        return rels or []

    mock.fetch_all.side_effect = _fetch
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

    def test_scoped_to_selected_project(self, runner: CliRunner) -> None:
        # (H) Only symbols of the selected project are reported even if the
        # (H) fetch surfaces rows from another prefix.
        rows: list[ResultRow] = [
            {
                "label": "Function",
                "name": "stray",
                "qualified_name": "other.mod.stray",
                "start_line": 1,
                "end_line": 2,
            },
        ]
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj", "other"], fetch_result=rows
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(
                app, ["dead-code", "--project-name", "myproj", "--format", "json"]
            )

        assert result.exit_code == 0
        assert json.loads(result.output) == []

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

    def test_entry_point_roots_matching_symbol(
        self, runner: CliRunner, dead_rows: list[ResultRow]
    ) -> None:
        # (H) -e marks matching qualified-name suffixes as reachable roots, so
        # (H) only the non-matching orphan is reported.
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=dead_rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(
                app, ["dead-code", "-e", "orphan_one", "--format", "json"]
            )

        assert result.exit_code == 0
        names = {row["qualified_name"] for row in json.loads(result.output)}
        assert names == {"myproj.mod.Thing.orphan_two"}

    def test_decorator_root_extends_defaults(self, runner: CliRunner) -> None:
        # (H) --decorator-root adds to the built-in root set (task, route, ...),
        # (H) so both decorated symbols are live and only the plain orphan reports.
        rows: list[ResultRow] = [
            {
                "label": "Function",
                "name": "custom",
                "qualified_name": "myproj.mod.custom",
                "decorators": ["@myhandler"],
                "start_line": 1,
                "end_line": 2,
            },
            {
                "label": "Function",
                "name": "job",
                "qualified_name": "myproj.mod.job",
                "decorators": ["@task"],
                "start_line": 4,
                "end_line": 5,
            },
            {
                "label": "Function",
                "name": "orphan",
                "qualified_name": "myproj.mod.orphan",
                "start_line": 7,
                "end_line": 8,
            },
        ]
        mock_ingestor = _make_mock_ingestor(projects=["myproj"], fetch_result=rows)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(
                app,
                ["dead-code", "--decorator-root", "myhandler", "--format", "json"],
            )

        assert result.exit_code == 0
        names = {row["qualified_name"] for row in json.loads(result.output)}
        assert names == {"myproj.mod.orphan"}

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

    @staticmethod
    def _test_flow_rows() -> tuple[list[ResultRow], list[ResultRow]]:
        # (H) A test function calls a production helper; nothing else reaches
        # (H) the helper.
        nodes: list[ResultRow] = [
            {
                "label": "Function",
                "name": "test_runs",
                "qualified_name": "myproj.tests.test_runs",
                "path": "proj/tests/test_mod.py",
                "start_line": 1,
                "end_line": 3,
            },
            {
                "label": "Function",
                "name": "helper",
                "qualified_name": "myproj.mod.helper",
                "path": "proj/mod.py",
                "start_line": 5,
                "end_line": 7,
            },
        ]
        rels: list[ResultRow] = [
            {
                "from_label": "Function",
                "from_qn": "myproj.tests.test_runs",
                "rel_type": "CALLS",
                "to_label": "Function",
                "to_qn": "myproj.mod.helper",
            },
        ]
        return nodes, rels

    def test_include_tests_default_roots_test_code(self, runner: CliRunner) -> None:
        # (H) With tests included (default), test functions are roots: the test
        # (H) and the helper it calls are both live, so nothing is reported.
        nodes, rels = self._test_flow_rows()
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], fetch_result=nodes, rels=rels
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--format", "json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_no_include_tests_reports_test_only_production_code(
        self, runner: CliRunner
    ) -> None:
        # (H) With tests excluded, production code reached only from tests is
        # (H) reported; the test function itself is filtered from the report
        # (H) (its only callers are excluded as roots, so it is pure noise).
        nodes, rels = self._test_flow_rows()
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], fetch_result=nodes, rels=rels
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(
                app, ["dead-code", "--no-include-tests", "--format", "json"]
            )

        assert result.exit_code == 0
        names = {row["qualified_name"] for row in json.loads(result.output)}
        assert names == {"myproj.mod.helper"}

    @staticmethod
    def _class_rows() -> list[ResultRow]:
        return [
            {
                "label": "Class",
                "name": "Orphan",
                "qualified_name": "myproj.mod.Orphan",
                "path": "proj/mod.py",
                "start_line": 1,
                "end_line": 4,
            },
        ]

    def test_classes_flag_includes_class_candidates(self, runner: CliRunner) -> None:
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], fetch_result=self._class_rows()
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--classes", "--format", "json"])

        assert result.exit_code == 0
        names = {row["qualified_name"] for row in json.loads(result.output)}
        assert names == {"myproj.mod.Orphan"}

    def test_classes_off_by_default(self, runner: CliRunner) -> None:
        mock_ingestor = _make_mock_ingestor(
            projects=["myproj"], fetch_result=self._class_rows()
        )
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["dead-code", "--format", "json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == []
