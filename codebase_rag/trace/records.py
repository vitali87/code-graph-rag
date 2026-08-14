"""Trace interchange format: JSONL records shared by every language tracer.

The first line of a trace file is a header record; every following line is an
aggregated call record. Paths are absolute as observed at runtime; ingestion
re-anchors them against the header's ``repo_root`` (or an override).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


class TraceFormatError(ValueError):
    """Raised when a trace file is malformed or has an unsupported version."""


@dataclass(frozen=True, slots=True)
class FramePoint:
    """One endpoint of an observed call, as reported by the runtime."""

    path: str
    qualname: str
    line: int


@dataclass(frozen=True, slots=True)
class TraceHeader:
    version: int
    language: str
    repo_root: str
    tracer: str


@dataclass(slots=True)
class CallRecord:
    """An aggregated caller/callee observation."""

    caller: FramePoint
    callee: FramePoint
    count: int
    workloads: tuple[str, ...]
    receiver_types: tuple[str, ...]


def _frame_to_json(point: FramePoint) -> dict[str, str | int]:
    return {
        cs.TRACE_KEY_PATH: point.path,
        cs.TRACE_KEY_QUALNAME: point.qualname,
        cs.TRACE_KEY_LINE: point.line,
    }


def _frame_from_json(raw: dict[str, object]) -> FramePoint:
    path = raw.get(cs.TRACE_KEY_PATH)
    qualname = raw.get(cs.TRACE_KEY_QUALNAME)
    line = raw.get(cs.TRACE_KEY_LINE)
    if (
        not isinstance(path, str)
        or not isinstance(qualname, str)
        or not isinstance(line, int)
        or isinstance(line, bool)
        or line < 0
    ):
        raise TraceFormatError(repr(raw))
    return FramePoint(path=path, qualname=qualname, line=line)


def write_trace_file(
    output: Path, header: TraceHeader, records: Iterable[CallRecord]
) -> None:
    with output.open("w", encoding="utf-8") as fh:
        header_row: dict[str, str | int] = {
            cs.TRACE_KEY_KIND: cs.TRACE_KIND_HEADER,
            cs.TRACE_KEY_VERSION: header.version,
            cs.TRACE_KEY_LANGUAGE: header.language,
            cs.TRACE_KEY_REPO_ROOT: header.repo_root,
            cs.TRACE_KEY_TRACER: header.tracer,
        }
        fh.write(json.dumps(header_row) + "\n")
        for record in records:
            row = {
                cs.TRACE_KEY_KIND: cs.TRACE_KIND_CALL,
                cs.TRACE_KEY_CALLER: _frame_to_json(record.caller),
                cs.TRACE_KEY_CALLEE: _frame_to_json(record.callee),
                cs.TRACE_KEY_COUNT: record.count,
                cs.TRACE_KEY_WORKLOADS: list(record.workloads),
                cs.TRACE_KEY_RECEIVER_TYPES: list(record.receiver_types),
            }
            fh.write(json.dumps(row) + "\n")


def _parse_header(trace_path: Path, line: str) -> TraceHeader:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        raise TraceFormatError(cs.TRACE_ERR_BAD_HEADER.format(path=trace_path)) from e
    if not isinstance(raw, dict) or raw.get(cs.TRACE_KEY_KIND) != cs.TRACE_KIND_HEADER:
        raise TraceFormatError(cs.TRACE_ERR_BAD_HEADER.format(path=trace_path))
    version = raw.get(cs.TRACE_KEY_VERSION)
    if version != cs.TRACE_FORMAT_VERSION:
        raise TraceFormatError(
            cs.TRACE_ERR_VERSION.format(
                path=trace_path,
                found=version,
                expected=cs.TRACE_FORMAT_VERSION,
            )
        )
    language = raw.get(cs.TRACE_KEY_LANGUAGE)
    repo_root = raw.get(cs.TRACE_KEY_REPO_ROOT)
    tracer = raw.get(cs.TRACE_KEY_TRACER)
    if (
        not isinstance(language, str)
        or not isinstance(repo_root, str)
        or not isinstance(tracer, str)
    ):
        raise TraceFormatError(cs.TRACE_ERR_BAD_HEADER.format(path=trace_path))
    return TraceHeader(
        version=version, language=language, repo_root=repo_root, tracer=tracer
    )


def _parse_record(trace_path: Path, line_no: int, line: str) -> CallRecord:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        raise TraceFormatError(
            cs.TRACE_ERR_BAD_RECORD.format(path=trace_path, line=line_no)
        ) from e
    caller = raw.get(cs.TRACE_KEY_CALLER) if isinstance(raw, dict) else None
    callee = raw.get(cs.TRACE_KEY_CALLEE) if isinstance(raw, dict) else None
    count = raw.get(cs.TRACE_KEY_COUNT) if isinstance(raw, dict) else None
    workloads = raw.get(cs.TRACE_KEY_WORKLOADS, []) if isinstance(raw, dict) else None
    receivers = (
        raw.get(cs.TRACE_KEY_RECEIVER_TYPES, []) if isinstance(raw, dict) else None
    )
    if (
        not isinstance(caller, dict)
        or not isinstance(callee, dict)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or not isinstance(workloads, list)
        or not isinstance(receivers, list)
    ):
        raise TraceFormatError(
            cs.TRACE_ERR_BAD_RECORD.format(path=trace_path, line=line_no)
        )
    try:
        return CallRecord(
            caller=_frame_from_json(caller),
            callee=_frame_from_json(callee),
            count=count,
            workloads=tuple(str(w) for w in workloads),
            receiver_types=tuple(str(r) for r in receivers),
        )
    except TraceFormatError as e:
        raise TraceFormatError(
            cs.TRACE_ERR_BAD_RECORD.format(path=trace_path, line=line_no)
        ) from e


def read_trace_file(trace_path: Path) -> tuple[TraceHeader, Iterator[CallRecord]]:
    """Parse a trace file, returning its header and a lazy record iterator."""
    with trace_path.open("r", encoding="utf-8") as fh:
        first = fh.readline()
    if not first.strip():
        raise TraceFormatError(cs.TRACE_ERR_BAD_HEADER.format(path=trace_path))
    header = _parse_header(trace_path, first)

    def _records() -> Iterator[CallRecord]:
        with trace_path.open("r", encoding="utf-8") as fh:
            fh.readline()
            for line_no, line in enumerate(fh, start=2):
                if line.strip():
                    yield _parse_record(trace_path, line_no, line)

    return header, _records()
