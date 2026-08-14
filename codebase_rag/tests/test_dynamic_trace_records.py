# The trace interchange parser must reject malformed records instead of
# letting corrupted values (bool masquerading as int, non-positive counts)
# flow into graph edge properties (issue #1246).

from __future__ import annotations

import json

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.records import TraceFormatError, read_trace_file


def _frame(line: object = 3) -> dict[str, object]:
    return {
        cs.TRACE_KEY_PATH: "/repo/pkg/mod.py",
        cs.TRACE_KEY_QUALNAME: "fn",
        cs.TRACE_KEY_LINE: line,
    }


def _write_trace(tmp_path, count: object = 1, line: object = 3):
    header = {
        cs.TRACE_KEY_KIND: cs.TRACE_KIND_HEADER,
        cs.TRACE_KEY_VERSION: cs.TRACE_FORMAT_VERSION,
        cs.TRACE_KEY_LANGUAGE: cs.TRACE_LANGUAGE_PYTHON,
        cs.TRACE_KEY_REPO_ROOT: "/repo",
        cs.TRACE_KEY_TRACER: cs.TRACE_TOOL_NAME,
    }
    record = {
        cs.TRACE_KEY_KIND: cs.TRACE_KIND_CALL,
        cs.TRACE_KEY_CALLER: _frame(),
        cs.TRACE_KEY_CALLEE: _frame(line),
        cs.TRACE_KEY_COUNT: count,
        cs.TRACE_KEY_WORKLOADS: [],
        cs.TRACE_KEY_RECEIVER_TYPES: [],
    }
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(json.dumps(header) + "\n" + json.dumps(record) + "\n")
    return trace_path


def test_valid_record_parses(tmp_path):
    _header, records = read_trace_file(_write_trace(tmp_path))
    assert [r.count for r in records] == [1]


@pytest.mark.parametrize("count", [True, False, 0, -1, "3", None, 1.5])
def test_non_positive_or_non_int_count_is_rejected(tmp_path, count):
    _header, records = read_trace_file(_write_trace(tmp_path, count=count))
    with pytest.raises(TraceFormatError):
        list(records)


@pytest.mark.parametrize("line", [True, -1, "3", None])
def test_bool_or_negative_line_is_rejected(tmp_path, line):
    _header, records = read_trace_file(_write_trace(tmp_path, line=line))
    with pytest.raises(TraceFormatError):
        list(records)
