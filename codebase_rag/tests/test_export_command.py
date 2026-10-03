"""`cgr export`: project scope, deprecated options, output errors (issue #2410).

The graph is shared by every indexed repository, and `cgr export` could only
dump all of it. `--batch-size` sized a write buffer an export never fills,
`--no-json` could only fail, and a mistyped `-o` surfaced after the whole
graph had been read, as several screens of traceback with local variables.
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
import typer
from click.testing import Result
from loguru import logger
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import cli
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import GraphData, GraphMetadata, ResultRow
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo, save_workspace

GRAPH = GraphData(
    nodes=[{"node_id": 1, "labels": ["Project"], "properties": {"name": "alpha"}}],
    relationships=[],
    metadata=GraphMetadata(
        total_nodes=1, total_relationships=0, exported_at="2026-09-29T00:00:00+00:00"
    ),
)
INDEXED = ["alpha", "beta", "gamma"]


@pytest.fixture(autouse=True)
def _isolated_output(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    # A wide console keeps each message on one line whatever the tmp path's
    # length, so "one line" below is about what cgr prints, not wrapping.
    monkeypatch.setattr(cli.app_context, "console", Console(width=1000))
    monkeypatch.setattr(settings, "QUIET", settings.QUIET)
    yield
    # `-q` swaps loguru's sinks; the suite runs with none installed.
    logger.remove()


@pytest.fixture
def connect() -> Generator[MagicMock, None, None]:
    ingestor = MagicMock()
    ingestor.list_projects.return_value = INDEXED
    ingestor.export_graph_to_dict.return_value = GRAPH
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_connect.return_value.__enter__ = MagicMock(return_value=ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


def _ingestor(connect: MagicMock) -> MagicMock:
    return connect.return_value.__enter__.return_value


def _run(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _scope(connect: MagicMock) -> list[str]:
    call = _ingestor(connect).export_graph_to_dict.call_args
    if call.args:
        return list(call.args[0])
    return list(call.kwargs.get("project_names", []))


def _lines(result: Result) -> list[str]:
    return [line for line in result.output.splitlines() if line.strip()]


def _save_workspace(home: Path, name: str, projects: list[str]) -> None:
    save_workspace(
        WorkspaceConfig(
            name=name,
            repos=[
                WorkspaceRepo(path=f"/src/{project}", project_name=project)
                for project in projects
            ],
        ),
        home=home,
    )


# --- project scope ----------------------------------------------------------


def test_one_project_is_exported_on_its_own(connect: MagicMock, tmp_path: Path) -> None:
    out = tmp_path / "one.json"

    result = _run(["export", "-o", str(out), "-n", "alpha"])

    assert result.exit_code == 0, result.output
    assert _scope(connect) == ["alpha"]
    assert json.loads(out.read_text(encoding="utf-8")) == GRAPH
    assert "alpha" in result.output


def test_the_project_option_repeats_and_takes_the_long_name(
    connect: MagicMock, tmp_path: Path
) -> None:
    result = _run(
        [
            "export",
            "-o",
            str(tmp_path / "x.json"),
            "--project-name",
            "alpha",
            "-n",
            "gamma",
        ]
    )

    assert result.exit_code == 0, result.output
    assert _scope(connect) == ["alpha", "gamma"]


def test_a_workspace_exports_its_projects_once_each(
    connect: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path)
    _save_workspace(tmp_path, "shop", ["alpha", "beta"])

    result = _run(
        ["export", "-o", str(tmp_path / "x.json"), "-n", "alpha", "--workspace", "shop"]
    )

    assert result.exit_code == 0, result.output
    assert _scope(connect) == ["alpha", "beta"]


def test_an_unknown_project_is_an_error_that_names_the_indexed_ones(
    connect: MagicMock, tmp_path: Path
) -> None:
    out = tmp_path / "x.json"

    result = _run(["export", "-o", str(out), "-n", "nope"])

    assert result.exit_code == 1
    assert "nope" in result.output
    assert "alpha, beta, gamma" in result.output
    _ingestor(connect).export_graph_to_dict.assert_not_called()
    assert not out.exists()


def test_an_unknown_workspace_is_an_error(
    connect: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path)

    result = _run(["export", "-o", str(tmp_path / "x.json"), "--workspace", "nope"])

    assert result.exit_code == 1
    connect.assert_not_called()


@pytest.mark.parametrize("scope", [["-n", "  "], ["--workspace", "empty"]])
def test_a_scope_that_names_no_project_is_not_the_whole_graph(
    scope: list[str],
    connect: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path)
    _save_workspace(tmp_path, "empty", [])

    result = _run(["export", "-o", str(tmp_path / "x.json"), *scope])

    assert result.exit_code == 1
    connect.assert_not_called()


# --- deprecated options -----------------------------------------------------


def test_batch_size_is_ignored_with_a_deprecation_warning(
    connect: MagicMock, tmp_path: Path
) -> None:
    out = tmp_path / "x.json"

    result = _run(["export", "-o", str(out), "--batch-size", "5000"])

    assert result.exit_code == 0, result.output
    assert "--batch-size" in result.output
    assert "deprecated" in result.output
    assert out.exists()
    assert 5000 not in connect.call_args.args
    assert 5000 not in connect.call_args.kwargs.values()


def test_the_json_flag_still_works_with_a_deprecation_warning(
    connect: MagicMock, tmp_path: Path
) -> None:
    out = tmp_path / "x.json"

    result = _run(["export", "-o", str(out), "--json"])

    assert result.exit_code == 0, result.output
    assert "--json" in result.output
    assert "deprecated" in result.output
    assert out.exists()


def test_no_json_says_the_option_is_deprecated(
    connect: MagicMock, tmp_path: Path
) -> None:
    result = _run(["export", "-o", str(tmp_path / "x.bin"), "--no-json"])

    assert result.exit_code == 1
    assert "--no-json" in result.output
    assert "deprecated" in result.output
    connect.assert_not_called()


def test_the_help_lists_the_scope_and_hides_the_deprecated_options() -> None:
    result = _run(["export", "--help"])
    # CI forces colour, so rich wraps each option name in ANSI spans.
    help_text = click.unstyle(result.output)

    assert result.exit_code == 0
    assert "--project-name" in help_text
    assert "--workspace" in help_text
    assert "--batch-size" not in help_text
    assert "--no-json" not in help_text


# --- output errors ----------------------------------------------------------


def test_a_directory_output_fails_before_the_graph_is_read(
    connect: MagicMock, tmp_path: Path
) -> None:
    result = _run(["-q", "export", "-o", str(tmp_path)])

    assert result.exit_code == 1
    connect.assert_not_called()
    assert len(_lines(result)) == 1, result.output
    assert str(tmp_path) in result.output
    assert "Traceback" not in result.output


def test_an_output_below_a_file_fails_before_the_graph_is_read(
    connect: MagicMock, tmp_path: Path
) -> None:
    blocker = tmp_path / "notes.txt"
    blocker.write_text("x", encoding="utf-8")

    result = _run(["-q", "export", "-o", str(blocker / "graph.json")])

    assert result.exit_code == 1
    connect.assert_not_called()
    assert len(_lines(result)) == 1, result.output
    assert str(blocker) in result.output


def test_a_directory_without_write_access_fails_before_the_graph_is_read(
    connect: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Root ignores permission bits, so the refusal is simulated where the
    # check asks for it.
    locked = tmp_path / "locked"
    locked.mkdir()
    real_access = os.access

    def access(path: str | Path, mode: int) -> bool:
        return Path(path) != locked and real_access(path, mode)

    monkeypatch.setattr(os, "access", access)

    result = _run(["-q", "export", "-o", str(locked / "graph.json")])

    assert result.exit_code == 1
    connect.assert_not_called()
    assert len(_lines(result)) == 1, result.output
    assert str(locked) in result.output


def test_a_failed_export_is_one_line_without_a_traceback(
    connect: MagicMock, tmp_path: Path
) -> None:
    _ingestor(connect).export_graph_to_dict.side_effect = RuntimeError("boom")

    result = _run(["-q", "export", "-o", str(tmp_path / "x.json")])

    assert result.exit_code == 1
    assert _lines(result) == ["Failed to export graph: boom"], result.output


def test_the_traceback_of_a_failed_export_is_kept_at_debug(
    connect: MagicMock, tmp_path: Path
) -> None:
    _ingestor(connect).export_graph_to_dict.side_effect = RuntimeError("boom")
    records: list[tuple[str, bool]] = []
    logger.add(
        lambda message: records.append(
            (message.record["level"].name, message.record["exception"] is not None)
        ),
        level="DEBUG",
    )

    result = _run(["export", "-o", str(tmp_path / "x.json")])

    assert result.exit_code == 1
    with_traceback = {level for level, has_exception in records if has_exception}
    assert with_traceback == {"DEBUG"}


def test_start_checks_its_output_before_indexing(tmp_path: Path) -> None:
    with patch("codebase_rag.cli._start_update_graph") as update:
        result = _run(
            [
                "start",
                "--repo-path",
                str(tmp_path),
                "--update-graph",
                "--no-start-stack",
                "-o",
                str(tmp_path),
            ]
        )

    assert result.exit_code == 1
    update.assert_not_called()
    assert str(tmp_path) in result.output


def test_a_failed_export_after_a_sync_exits_outside_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "home")
    connection = MagicMock()
    connection.__enter__.return_value = MagicMock()
    connection.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch("codebase_rag.graph_updater.GraphUpdater"),
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.cli.export_graph_to_file", return_value=False),
        pytest.raises(typer.Exit),
    ):
        cli._run_graph_sync(
            repo=tmp_path,
            project_name="proj",
            project_named=True,
            batch_size=10,
            exclude=None,
            interactive_setup=False,
            output=str(tmp_path / "x.json"),
        )

    # The ingestor logs an exception leaving its block as a traceback, and
    # typer.Exit is one: the export already said what went wrong.
    assert connection.__exit__.call_args.args[0] is None


# --- what stays as it was ---------------------------------------------------


def test_without_a_scope_the_whole_graph_is_exported(
    connect: MagicMock, tmp_path: Path
) -> None:
    out = tmp_path / "x.json"

    result = _run(["export", "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert _scope(connect) == []
    _ingestor(connect).list_projects.assert_not_called()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == GRAPH
    assert "deprecated" not in result.output


def test_an_existing_file_is_overwritten(connect: MagicMock, tmp_path: Path) -> None:
    out = tmp_path / "x.json"
    out.write_text("stale", encoding="utf-8")

    result = _run(["export", "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8")) == GRAPH


def test_missing_parent_directories_are_still_created(
    connect: MagicMock, tmp_path: Path
) -> None:
    out = tmp_path / "a" / "b" / "x.json"

    result = _run(["export", "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()


# --- the ingestor's queries -------------------------------------------------


def _record_queries(
    rows: list[ResultRow],
) -> tuple[list[tuple[str, dict | None]], MagicMock]:
    seen: list[tuple[str, dict | None]] = []

    def fetch_all(query: str, params: dict | None = None) -> list[ResultRow]:
        seen.append((query, params))
        return rows

    return seen, MagicMock(side_effect=fetch_all)


def test_a_scoped_export_reads_what_the_projects_own() -> None:
    seen, fetch_all = _record_queries([])
    with patch.object(MemgraphIngestor, "fetch_all", fetch_all):
        data = MemgraphIngestor(host="localhost", port=7687).export_graph_to_dict(
            ["alpha", "beta"]
        )

    params = {"project_names": ["alpha", "beta"]}
    assert seen == [
        (cq.CYPHER_EXPORT_PROJECT_NODES, params),
        (cq.CYPHER_EXPORT_PROJECT_RELATIONSHIPS, params),
    ]
    assert data["metadata"].get("projects") == ["alpha", "beta"]


def test_an_unscoped_export_runs_the_whole_graph_queries() -> None:
    seen, fetch_all = _record_queries([])
    with patch.object(MemgraphIngestor, "fetch_all", fetch_all):
        data = MemgraphIngestor(host="localhost", port=7687).export_graph_to_dict()

    assert seen == [
        (cq.CYPHER_EXPORT_NODES, None),
        (cq.CYPHER_EXPORT_RELATIONSHIPS, None),
    ]
    assert "projects" not in data["metadata"]
