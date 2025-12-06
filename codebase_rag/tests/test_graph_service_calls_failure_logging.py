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
    # Mock the database connection to avoid actual database calls
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
    # Add some CALLS relationships
    # Simulate a scenario where some nodes don't exist
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

    # Mock the batch execution to simulate partial failures
    # First call succeeds, second and third fail (nodes don't exist)
    with patch.object(
        graph_service,
        "_execute_batch_with_return",
        return_value=[{"created": 1}, {"created": 0}, {"created": 0}],
    ):
        graph_service.flush_relationships()

    # Check that failure logging is correct
    # Expected: 2 failures out of 3 attempts in this single batch
    log_text = "\n".join(log_messages)
    assert "Failed to create 2 CALLS relationships" in log_text
    assert "nodes may not exist" in log_text

    # Verify samples are logged
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
    # Create two different batches by using different relationship patterns
    # Batch 1: Method -> Method CALLS
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

    # Batch 2: Function -> Function CALLS (different pattern, different batch)
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

    # Mock the batch execution to simulate:
    # - First batch (Method->Method): 1 success, 1 failure
    # - Second batch (Function->Function): 1 success, 1 failure
    call_count = 0

    def mock_execute_batch(
        query: str, params_list: list[dict[str, Any]]
    ) -> list[dict[str, int]]:
        nonlocal call_count
        call_count += 1
        # Each batch has 2 items: 1 succeeds, 1 fails
        return [{"created": 1}, {"created": 0}]

    with patch.object(
        graph_service, "_execute_batch_with_return", side_effect=mock_execute_batch
    ):
        graph_service.flush_relationships()

    # Critical assertion: Each batch should report exactly 1 failure
    # If the bug existed, the second batch would report:
    #   failed = total_attempted - total_successful = 4 - 2 = 2 (WRONG!)
    # With the fix, each batch correctly reports:
    #   failed = len(params_list) - batch_successful = 2 - 1 = 1 (CORRECT!)

    log_text = "\n".join(log_messages)

    # Count how many times we logged "Failed to create 1 CALLS relationships"
    failure_count = log_text.count("Failed to create 1 CALLS relationships")

    # We should have exactly 2 such logs (one per batch)
    assert failure_count == 2, (
        f"Expected 2 batches to each report 1 failure, but found {failure_count} occurrences in logs:\n{log_text}"
    )


def test_calls_success_no_failure_logging(
    graph_service: MemgraphIngestor, log_messages: list[str]
) -> None:
    """Test that successful CALLS don't trigger failure warnings."""
    # Add CALLS relationships that will all succeed
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

    # Mock successful execution (all relationships created)
    with patch.object(
        graph_service,
        "_execute_batch_with_return",
        return_value=[{"created": 1}, {"created": 1}],
    ):
        graph_service.flush_relationships()

    # No failure warnings should be logged
    log_text = "\n".join(log_messages)
    assert "Failed to create" not in log_text
    assert "nodes may not exist" not in log_text


def test_non_calls_relationships_no_failure_logging(
    graph_service: MemgraphIngestor, log_messages: list[str]
) -> None:
    """Test that failures in non-CALLS relationships don't trigger CALLS-specific logging."""
    # Add some IMPORTS relationships (not CALLS)
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

    # Mock partial failure
    with patch.object(
        graph_service,
        "_execute_batch_with_return",
        return_value=[{"created": 1}, {"created": 0}],
    ):
        graph_service.flush_relationships()

    # CALLS-specific warning should NOT appear
    log_text = "\n".join(log_messages)
    assert "Failed to create" not in log_text or "CALLS" not in log_text
