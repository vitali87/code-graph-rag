"""Convert Xdebug computerized traces to the trace interchange format.

``xdebug.mode=trace`` with ``xdebug.trace_format=1`` records every call as a
tab-separated entry row carrying the resolved function name (with the
concrete receiver class, so ``$animal->sound()`` shows which implementation
ran), whether the function is user-defined, and the call site's file and
line. Unlike the sampled JS and .NET converters, these are exact call
records: counts are true invocation counts.

Xdebug never reports where a function is defined, only where it was called
from. Conversion therefore runs in two passes: the first collects every
call with its stack parent, and infers each frame's defining file and an
in-body line from its children's call sites (a call made *by* a function
necessarily sits inside its body). The second pass emits edges whose
endpoints carry those recovered positions, which matters because PHP
qualified names are path-derived (the namespace declaration is ignored), so
span-based resolution against file plus line is far more reliable than
names alone.

Internal functions (``call_user_func``, callback-taking array functions)
and file-inclusion pseudo-frames are glue, not endpoints; edges see through
them to the nearest user-defined ancestor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .. import constants as cs
from .records import (
    CallRecord,
    FramePoint,
    TraceFormatError,
    TraceHeader,
    write_trace_file,
)

if TYPE_CHECKING:
    from pathlib import Path

_ENTRY_MIN_COLUMNS = 10
_COL_LEVEL = 0
_COL_KIND = 2
_COL_NAME = 5
_COL_USER_DEFINED = 6
_COL_FILENAME = 8
_COL_LINE = 9
_KIND_ENTRY = "0"


@dataclass(slots=True)
class _Call:
    """One observed invocation, linked to the call that made it."""

    qualname: str
    call_file: str
    call_line: int
    transparent: bool
    parent: int | None
    defining_file: str = ""
    defining_line: int = 0
    children_seen: bool = field(default=False)


def _parse_calls(lines: list[str]) -> list[_Call]:
    calls: list[_Call] = []
    stack: dict[int, int] = {}
    for line in lines:
        columns = line.split("\t")
        if len(columns) < _ENTRY_MIN_COLUMNS or columns[_COL_KIND] != _KIND_ENTRY:
            continue
        try:
            level = int(columns[_COL_LEVEL])
            call_line = int(columns[_COL_LINE])
        except ValueError:
            continue
        name = columns[_COL_NAME]
        call_file = columns[_COL_FILENAME]
        user_defined = columns[_COL_USER_DEFINED] == "1"
        transparent = not user_defined or name in cs.TRACE_XDEBUG_GLUE_FUNCTIONS

        parent_index = stack.get(level - 1)
        call = _Call(
            qualname=name,
            call_file=call_file,
            call_line=call_line,
            transparent=transparent,
            parent=parent_index,
        )
        if name == cs.TRACE_XDEBUG_MAIN:
            call.defining_file = call_file
        index = len(calls)
        calls.append(call)
        stack[level] = index

        # This call's site lies inside its parent's body, revealing where
        # the parent is defined. Glue frames pass the position through.
        ancestor = parent_index
        while ancestor is not None and calls[ancestor].transparent:
            ancestor = calls[ancestor].parent
        if ancestor is not None and not calls[ancestor].children_seen:
            calls[ancestor].children_seen = True
            calls[ancestor].defining_file = call_file
            calls[ancestor].defining_line = call_line
    return calls


def _nearest_opaque_ancestor(calls: list[_Call], call: _Call) -> _Call | None:
    ancestor = call.parent
    while ancestor is not None and calls[ancestor].transparent:
        ancestor = calls[ancestor].parent
    return calls[ancestor] if ancestor is not None else None


def convert_xdebug_trace(
    trace_path: Path,
    output: Path,
    workload: str | None = None,
) -> int:
    """Write ``trace_path``'s call edges to ``output``; returns record count."""
    lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not any(line.startswith(cs.TRACE_XDEBUG_FORMAT_LINE) for line in lines[:5]):
        raise TraceFormatError(cs.TRACE_ERR_BAD_XDEBUG.format(path=trace_path))

    calls = _parse_calls(lines)
    # Runtime names embed the namespace, so they identify one declaration;
    # any invocation that revealed its defining position speaks for all of
    # them, keeping repeated calls aggregated onto a single edge.
    defining: dict[str, tuple[str, int]] = {}
    for call in calls:
        if call.defining_file and call.qualname not in defining:
            defining[call.qualname] = (call.defining_file, call.defining_line)
    edges: dict[tuple[FramePoint, FramePoint], int] = {}
    for call in calls:
        if call.transparent:
            continue
        parent = _nearest_opaque_ancestor(calls, call)
        if parent is None:
            continue
        caller = FramePoint(
            qualname=parent.qualname, path=call.call_file, line=call.call_line
        )
        defining_file, defining_line = defining.get(call.qualname, ("", 0))
        callee = FramePoint(
            qualname=call.qualname, path=defining_file, line=defining_line
        )
        key = (caller, callee)
        edges[key] = edges.get(key, 0) + 1

    workloads = (workload,) if workload else ()
    records = [
        CallRecord(
            caller=caller,
            callee=callee,
            count=count,
            workloads=workloads,
            receiver_types=(),
        )
        for (caller, callee), count in edges.items()
    ]
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_PHP,
        repo_root="",
        tracer=cs.TRACE_TOOL_NAME_XDEBUG,
    )
    write_trace_file(output, header, records)
    return len(records)
