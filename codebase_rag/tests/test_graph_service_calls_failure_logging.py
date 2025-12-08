from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from codebase_rag.services.graph_service import MemgraphIngestor


@pytest.fixture
def graph_service() -> MemgraphIngestor:
    """Create a MemgraphIngestor instance with mocked database."""
    ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=100)
    ingestor.conn = MagicMock()
    return ingestor


@pytest.fixture
def log_messages() -> Generator[list[str], None, None]:
    """Capture log messages using a custom sink."""
    messages: list[str] = []

    def sink(message: Any) -> None:
        messages.append(str(message))

    handler_id = logger.add(sink, format="{message}")
    yield messages
    logger.remove(handler_id)


def test_calls_failure_logging_single_batch(
    graph_service: MemgraphIngestor, log_messages: list[str]
) -> None:
    """Test that CALLS failures are logged correctly for a single batch.

    This validates that the failure count is calculated correctly using
    batch-specific counts, not cumulative totals.
    """
    graph_service.ensure_relationship_batch(
        ("Method", "qualified_name", "project.module.ClassA.methodA()"),
        "CALLS",
        ("Method", "qualified_name", "project.module.ClassB.methodB()"),
    )
    graph_service.ensure_relationship_batch(
        ("Method", "qualified_name", "project.module.ClassA.methodA()"),
        "CALLS",
        ("Method", "qualified_name", "project.module.NonExistent.missing()"),
    )
    graph_service.ensure_relationship_batch(
        ("Method", "qualified_name", "project.module.ClassC.methodC()"),
        "CALLS",
        ("Method", "qualified_name", "project.module.AlsoMissing.gone()"),
    )

    with patch.object(
        graph_service,
        "_execute_batch_with_return",
        return_value=[{"created": 1}, {"created": 0}, {"created": 0}],
    ):
        graph_service.flush_relationships()

    log_text = "\n".join(log_messages)
    assert "Failed to create 2 CALLS relationships" in log_text
    assert "nodes may not exist" in log_text

    assert "Sample 1:" in log_text or "Sample 2:" in log_text


def test_calls_failure_logging_multiple_batches(
    graph_service: MemgraphIngestor, log_messages: list[str]
) -> None:
    """Test that CALLS failures are logged correctly across multiple batches.

    This is the critical test case that validates the bug fix:
    - Previously, the code used cumulative totals (total_attempted - total_successful)
    - This would incorrectly report failures for batches after the first one
    - Now it correctly uses batch-specific counts (len(params_list) - batch_successful)
    """
    graph_service.ensure_relationship_batch(
        ("Method", "qualified_name", "project.module.ClassA.methodA()"),
        "CALLS",
        ("Method", "qualified_name", "project.module.ClassB.methodB()"),
    )
    graph_service.ensure_relationship_batch(
        ("Method", "qualified_name", "project.module.ClassA.methodA()"),
        "CALLS",
        ("Method", "qualified_name", "project.module.Missing1.missing1()"),
    )

    graph_service.ensure_relationship_batch(
        ("Function", "qualified_name", "project.module.funcA"),
        "CALLS",
        ("Function", "qualified_name", "project.module.funcB"),
    )
    graph_service.ensure_relationship_batch(
        ("Function", "qualified_name", "project.module.funcA"),
        "CALLS",
        ("Function", "qualified_name", "project.module.missing2"),
    )

    call_count = 0

    def mock_execute_batch(
        query: str, params_list: list[dict[str, Any]]
    ) -> list[dict[str, int]]:
        nonlocal call_count
        call_count += 1
        return [{"created": 1}, {"created": 0}]

    with patch.object(
        graph_service, "_execute_batch_with_return", side_effect=mock_execute_batch
    ):
        graph_service.flush_relationships()

    log_text = "\n".join(log_messages)

    failure_count = log_text.count("Failed to create 1 CALLS relationships")

    assert failure_count == 2, (
        f"Expected 2 batches to each report 1 failure, but found {failure_count} occurrences in logs:\n{log_text}"
    )


def test_calls_success_no_failure_logging(
    graph_service: MemgraphIngestor, log_messages: list[str]
) -> None:
    """Test that successful CALLS don't trigger failure warnings."""
    graph_service.ensure_relationship_batch(
        ("Method", "qualified_name", "project.module.ClassA.methodA()"),
        "CALLS",
        ("Method", "qualified_name", "project.module.ClassB.methodB()"),
    )
    graph_service.ensure_relationship_batch(
        ("Method", "qualified_name", "project.module.ClassC.methodC()"),
        "CALLS",
        ("Method", "qualified_name", "project.module.ClassD.methodD()"),
    )

    with patch.object(
        graph_service,
        "_execute_batch_with_return",
        return_value=[{"created": 1}, {"created": 1}],
    ):
        graph_service.flush_relationships()

    log_text = "\n".join(log_messages)
    assert "Failed to create" not in log_text
    assert "nodes may not exist" not in log_text


def test_non_calls_relationships_no_failure_logging(
    graph_service: MemgraphIngestor, log_messages: list[str]
) -> None:
    """Test that failures in non-CALLS relationships don't trigger CALLS-specific logging."""
    graph_service.ensure_relationship_batch(
        ("Module", "qualified_name", "project.moduleA"),
        "IMPORTS",
        ("Module", "qualified_name", "project.moduleB"),
    )
    graph_service.ensure_relationship_batch(
        ("Module", "qualified_name", "project.moduleA"),
        "IMPORTS",
        ("Module", "qualified_name", "project.missing"),
    )

    with patch.object(
        graph_service,
        "_execute_batch_with_return",
        return_value=[{"created": 1}, {"created": 0}],
    ):
        graph_service.flush_relationships()

    log_text = "\n".join(log_messages)
    assert "Failed to create" not in log_text or "CALLS" not in log_text
