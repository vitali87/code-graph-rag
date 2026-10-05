"""The relationship flush summary's three numbers add up (issue #2879).

`total` was the rows buffered while `failed` was attempted minus written, so
the rows of a pattern group whose write raised were in the total but in
neither count, and a row-overcounting write made `failed` negative.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import RelBatchRow

_CALLS = cs.RelationshipType.CALLS.value


def _ingestor_with_rows() -> MemgraphIngestor:
    ingestor = MemgraphIngestor(host="127.0.0.1", port=7999)
    ingestor.ensure_relationship_batch(
        ("Function", cs.KEY_QUALIFIED_NAME, "m.f"),
        _CALLS,
        ("Function", cs.KEY_QUALIFIED_NAME, "m.g"),
    )
    for callee in ("m.C.a", "m.C.b"):
        ingestor.ensure_relationship_batch(
            ("Method", cs.KEY_QUALIFIED_NAME, "m.C.run"),
            _CALLS,
            ("Method", cs.KEY_QUALIFIED_NAME, callee),
        )
    return ingestor


def _summary(ingestor: MemgraphIngestor) -> str:
    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        with pytest.raises(RuntimeError):
            ingestor.flush_relationships()
    finally:
        logger.remove(handler)
    return next(line for line in lines if "relationships (" in line)


def test_rows_of_a_group_that_raised_count_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingestor = _ingestor_with_rows()

    def flush(
        self: MemgraphIngestor,
        pattern: tuple[str, str, str, str, str],
        params_list: list[RelBatchRow],
        conn: object = None,
    ) -> tuple[int, int]:
        if pattern[0] == "Method":
            raise RuntimeError("write failed")
        return len(params_list), len(params_list)

    monkeypatch.setattr(MemgraphIngestor, "_flush_rel_pattern_group", flush)
    summary = _summary(ingestor)
    assert "Flushed 3 relationships (1 successful, 2 failed)." in summary, summary


def test_every_row_carries_its_own_ordinal() -> None:
    # Two identical rows are two writes; the count must not fold them into
    # one, and a row matching several edges must not count more than once.
    ingestor = MemgraphIngestor(host="127.0.0.1", port=7999)
    cursor = MagicMock()
    cursor.description = [MagicMock(name="created")]
    cursor.description[0].name = cs.KEY_CREATED
    cursor.fetchall.return_value = [(2,)]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    row = RelBatchRow(from_val="m.f", to_val="m.g", props={})
    pattern = ("Function", cs.KEY_QUALIFIED_NAME, _CALLS, "Function", "qualified_name")
    ingestor._execute_rel_pattern_query(pattern, "RETURN 1", [row, row], conn)
    sent = cursor.execute.call_args.args[1]["batch"]
    ordinals = [r[cs.KEY_ROW_INDEX] for r in sent]
    assert ordinals == [0, 1], sent
