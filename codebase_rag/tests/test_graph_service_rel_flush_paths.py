# flush_relationships flushes every pattern group, serially or on the thread
# pool, sums what each group created, keeps going past a failing group, and
# re-raises the first failure once every group has run.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import RelBatchRow


def _ingestor_with_two_groups() -> MemgraphIngestor:
    ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=100)
    ingestor.conn = MagicMock()
    ingestor.ensure_relationship_batch(
        ("Function", "qualified_name", "p.a"),
        "CALLS",
        ("Function", "qualified_name", "p.b"),
    )
    ingestor.ensure_relationship_batch(
        ("Module", "qualified_name", "p"),
        "DEFINES",
        ("Function", "qualified_name", "p.a"),
    )
    return ingestor


def test_serial_flush_sums_every_group() -> None:
    ingestor = _ingestor_with_two_groups()
    with patch.object(
        MemgraphIngestor, "_flush_rel_pattern_group", return_value=(1, 1)
    ) as flush:
        ingestor.flush_relationships()
    assert flush.call_count == 2
    assert ingestor._rel_count == 0


def test_serial_flush_runs_every_group_then_raises_the_first_failure() -> None:
    ingestor = _ingestor_with_two_groups()
    boom = RuntimeError("store refused")
    with (
        patch.object(
            MemgraphIngestor,
            "_flush_rel_pattern_group",
            side_effect=[boom, (1, 1)],
        ) as flush,
        pytest.raises(RuntimeError) as raised,
    ):
        ingestor.flush_relationships()
    assert raised.value is boom
    assert flush.call_count == 2
    assert ingestor._rel_count == 0


def test_parallel_flush_runs_every_group_then_raises_the_first_failure() -> None:
    ingestor = _ingestor_with_two_groups()
    boom = RuntimeError("store refused")
    calls: list[tuple[str, str, str, str, str]] = []

    def flush_group(
        pattern: tuple[str, str, str, str, str], params: list[RelBatchRow]
    ) -> tuple[int, int]:
        calls.append(pattern)
        if pattern[2] == "CALLS":
            raise boom
        return 1, 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        ingestor._executor = executor
        with (
            patch.object(
                MemgraphIngestor,
                "_flush_rel_group_with_own_conn",
                side_effect=flush_group,
            ),
            pytest.raises(RuntimeError) as raised,
        ):
            ingestor.flush_relationships()
        ingestor._executor = None
    assert raised.value is boom
    assert len(calls) == 2
