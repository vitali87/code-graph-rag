from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor


@pytest.fixture
def updater(temp_repo: Path) -> GraphUpdater:
    mock = MagicMock(spec=MemgraphIngestor)
    mock.fetch_all = MagicMock(return_value=[])
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=mock,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )


@pytest.fixture
def log_messages() -> Generator[list[str], None, None]:
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(str(msg)), level="DEBUG")
    yield messages
    logger.remove(handler_id)


class TestReconcileEmbeddings:
    def test_noop_when_expected_empty(self, updater: GraphUpdater) -> None:
        mock_fn = MagicMock()
        updater._reconcile_embeddings(set(), mock_fn)
        mock_fn.assert_not_called()

    def test_logs_ok_when_all_found(
        self, updater: GraphUpdater, log_messages: list[str]
    ) -> None:
        expected = {1, 2, 3}
        mock_fn = MagicMock(return_value={1, 2, 3})

        updater._reconcile_embeddings(expected, mock_fn)

        mock_fn.assert_called_once_with(expected)
        combined = "\n".join(log_messages)
        assert "all 3 expected embeddings found" in combined

    def test_logs_warning_when_ids_missing(
        self, updater: GraphUpdater, log_messages: list[str]
    ) -> None:
        expected = {1, 2, 3, 4, 5}
        mock_fn = MagicMock(return_value={1, 3})

        updater._reconcile_embeddings(expected, mock_fn)

        combined = "\n".join(log_messages)
        assert "3 of 5 embeddings missing" in combined

    def test_sample_ids_in_warning(
        self, updater: GraphUpdater, log_messages: list[str]
    ) -> None:
        expected = {10, 20, 30}
        mock_fn = MagicMock(return_value={10})

        updater._reconcile_embeddings(expected, mock_fn)

        combined = "\n".join(log_messages)
        assert "20" in combined
        assert "30" in combined

    def test_handles_verify_fn_exception(
        self, updater: GraphUpdater, log_messages: list[str]
    ) -> None:
        mock_fn = MagicMock(side_effect=RuntimeError("connection lost"))

        updater._reconcile_embeddings({1, 2}, mock_fn)

        combined = "\n".join(log_messages).lower()
        assert "reconciliation check failed" in combined

    def test_sample_limited_to_ten(
        self, updater: GraphUpdater, log_messages: list[str]
    ) -> None:
        expected = set(range(20))
        mock_fn = MagicMock(return_value=set())

        updater._reconcile_embeddings(expected, mock_fn)

        combined = "\n".join(log_messages)
        assert "20 of 20 embeddings missing" in combined
