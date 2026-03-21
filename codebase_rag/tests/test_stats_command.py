from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.types_defs import ResultRow


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_node_results() -> list[ResultRow]:
    return [
        {"labels": ["Function"], "count": 100},
        {"labels": ["Class"], "count": 50},
        {"labels": ["Module"], "count": 30},
    ]


@pytest.fixture
def mock_rel_results() -> list[ResultRow]:
    return [
        {"type": "CALLS", "count": 200},
        {"type": "DEFINES", "count": 80},
    ]


def _make_mock_ingestor(*fetch_side_effects: list[ResultRow]) -> MagicMock:
    mock = MagicMock()
    mock.fetch_all.side_effect = list(fetch_side_effects)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestStatsCommand:
    def test_stats_displays_node_table(
        self,
        runner: CliRunner,
        mock_node_results: list[ResultRow],
        mock_rel_results: list[ResultRow],
    ) -> None:
        mock_ingestor = _make_mock_ingestor(mock_node_results, mock_rel_results)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["stats"])

        assert result.exit_code == 0
        assert "Function" in result.output
        assert "Class" in result.output
        assert "Module" in result.output

    def test_stats_displays_relationship_table(
        self,
        runner: CliRunner,
        mock_node_results: list[ResultRow],
        mock_rel_results: list[ResultRow],
    ) -> None:
        mock_ingestor = _make_mock_ingestor(mock_node_results, mock_rel_results)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["stats"])

        assert result.exit_code == 0
        assert "CALLS" in result.output
        assert "DEFINES" in result.output

    def test_stats_displays_totals(
        self,
        runner: CliRunner,
        mock_node_results: list[ResultRow],
        mock_rel_results: list[ResultRow],
    ) -> None:
        mock_ingestor = _make_mock_ingestor(mock_node_results, mock_rel_results)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["stats"])

        assert result.exit_code == 0
        assert "180" in result.output
        assert "280" in result.output

    def test_stats_handles_empty_graph(
        self,
        runner: CliRunner,
    ) -> None:
        mock_ingestor = _make_mock_ingestor([], [])
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["stats"])

        assert result.exit_code == 0
        assert "0" in result.output

    def test_stats_handles_connection_error(
        self,
        runner: CliRunner,
    ) -> None:
        with patch(
            "codebase_rag.cli.connect_memgraph",
            side_effect=ConnectionError("Cannot connect"),
        ):
            result = runner.invoke(app, ["stats"])

        assert result.exit_code == 1
        assert "Failed" in result.output

    def test_stats_handles_multi_label_nodes(
        self,
        runner: CliRunner,
        mock_rel_results: list[ResultRow],
    ) -> None:
        node_results: list[ResultRow] = [
            {"labels": ["Function", "Exported"], "count": 10},
        ]
        mock_ingestor = _make_mock_ingestor(node_results, mock_rel_results)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["stats"])

        assert result.exit_code == 0
        assert "Function:Exported" in result.output

    def test_stats_handles_empty_labels(
        self,
        runner: CliRunner,
        mock_rel_results: list[ResultRow],
    ) -> None:
        node_results: list[ResultRow] = [
            {"labels": [], "count": 5},
        ]
        mock_ingestor = _make_mock_ingestor(node_results, mock_rel_results)
        with patch("codebase_rag.cli.connect_memgraph", return_value=mock_ingestor):
            result = runner.invoke(app, ["stats"])

        assert result.exit_code == 0
        assert "Unknown" in result.output
