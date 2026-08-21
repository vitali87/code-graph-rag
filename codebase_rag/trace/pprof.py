"""Convert Go pprof CPU profiles to the trace interchange format.

``go test -cpuprofile`` and ``runtime/pprof`` write gzipped protobuf
profiles whose samples are observed stacks: locations reference functions
with symbol names, source files, and declaration lines. Parent/child
adjacency within a sampled stack is a caller/callee relationship the
sampler actually saw, so dispatch through interface values and function
values appears whenever samples landed there. Counts are sample counts,
not call counts.

The wire decoding below is deliberately dependency-free: pprof's schema is
small and stable, and only samples, locations, lines, functions, and the
string table are needed. Frames outside the repository (the Go runtime,
the standard library, module cache) and vendored code are glue; edges see
through them to the nearest project frame.

Go symbol names carry package paths, receivers, and generic instantiations
(``example.com/pkg.(*Dog).Sound``, ``main.Map[go.shape.int]``); resolution
is span-first against declaration lines, so names normalise to their bare
member form and compiler-generated closure symbols (``runAll.func1``)
become ``<anonymous>``. Traced builds should disable inlining
(``-gcflags=all=-l``) or inlined callees vanish from the profile.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
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
    from collections.abc import Callable, Iterator
    from pathlib import Path

_Sample = tuple[list[int], int]

_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LEN = 2
_WIRE_FIXED32 = 5

_GO_CLOSURE_SEGMENT = re.compile(r"^func\d+(\.\d+)*$")
_GO_EXCLUDED_DIR = "vendor"


def _fields(data: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    position = 0
    length = len(data)
    while position < length:
        tag, position = _varint(data, position)
        field, wire = tag >> 3, tag & 0x7
        if wire == _WIRE_VARINT:
            value, position = _varint(data, position)
            yield field, wire, value
        elif wire == _WIRE_LEN:
            size, position = _varint(data, position)
            yield field, wire, data[position : position + size]
            position += size
        elif wire == _WIRE_FIXED64:
            yield field, wire, int.from_bytes(data[position : position + 8], "little")
            position += 8
        elif wire == _WIRE_FIXED32:
            yield field, wire, int.from_bytes(data[position : position + 4], "little")
            position += 4
        else:
            raise TraceFormatError(str(wire))


def _varint(data: bytes, position: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if position >= len(data):
            raise TraceFormatError("truncated varint")
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7


def _repeated_uint(wire: int, value: bytes | int) -> list[int]:
    """A repeated uint64 field arrives packed or one element at a time."""
    if wire == _WIRE_VARINT and isinstance(value, int):
        return [value]
    if wire == _WIRE_LEN and isinstance(value, bytes):
        out = []
        position = 0
        while position < len(value):
            element, position = _varint(value, position)
            out.append(element)
        return out
    return []


@dataclass(slots=True)
class _Function:
    name_index: int = 0
    filename_index: int = 0
    start_line: int = 0


def _parse_function(payload: bytes) -> tuple[int, _Function]:
    function_id = 0
    function = _Function()
    for field, _wire, value in _fields(payload):
        if field == 1 and isinstance(value, int):
            function_id = value
        elif field == 2 and isinstance(value, int):
            function.name_index = value
        elif field == 4 and isinstance(value, int):
            function.filename_index = value
        elif field == 5 and isinstance(value, int):
            function.start_line = value
    return function_id, function


def _parse_location(payload: bytes) -> tuple[int, list[int]]:
    """A location's function ids, innermost inline frame first."""
    location_id = 0
    function_ids: list[int] = []
    for field, _wire, value in _fields(payload):
        if field == 1 and isinstance(value, int):
            location_id = value
        elif field == 4 and isinstance(value, bytes):
            for line_field, _lw, line_value in _fields(value):
                if line_field == 1 and isinstance(line_value, int):
                    function_ids.append(line_value)
    return location_id, function_ids


def _parse_sample(payload: bytes) -> tuple[list[int], int]:
    location_ids: list[int] = []
    weight = 0
    for field, wire, value in _fields(payload):
        if field == 1:
            location_ids.extend(_repeated_uint(wire, value))
        elif field == 2 and not weight:
            values = _repeated_uint(wire, value)
            weight = values[0] if values else 0
    return location_ids, max(weight, 1)


def _matching_bracket(text: str, start: int) -> int:
    """Index of the ``]`` closing the ``[`` at ``start``, or -1 if unbalanced."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _strip_generics(tail: str) -> str:
    """Drop ``[...]`` generic instantiations, keeping what follows them.

    `(*Cache[go.shape.string]).Get` must keep `.Get`. Brackets nest in
    shape types, so remove balanced groups rather than up to the first `]`;
    an unbalanced group truncates the remainder.
    """
    while True:
        start = tail.find("[")
        if start < 0:
            return tail
        end = _matching_bracket(tail, start)
        if end < 0:
            return tail[:start]
        tail = tail[:start] + tail[end + 1 :]


def _bare_name(symbol: str) -> str:
    """``example.com/pkg.(*Dog).Sound`` as the member name ``Sound``.

    Span resolution against declaration lines carries identity; the name is
    a tiebreak, so package paths, receivers, and generic instantiations
    drop. Compiler-generated closure segments mark the frame anonymous.
    """
    tail = _strip_generics(symbol.rsplit("/", 1)[-1])
    segments = [s for s in tail.split(".") if s and not s.startswith("(")]
    if not segments:
        return cs.TRACE_QUALNAME_ANONYMOUS
    # A nested closure surfaces as `main.runAll.func1.1`; its final segment is
    # a bare number, so test every trailing run of segments, not just the leaf.
    if any(
        _GO_CLOSURE_SEGMENT.fullmatch(".".join(segments[index:]))
        for index in range(len(segments))
    ):
        return cs.TRACE_QUALNAME_ANONYMOUS
    return segments[-1]


def _decompress(raw: bytes, profile_path: Path) -> bytes:
    """Gunzip a gzipped profile; malformed input becomes a TraceFormatError."""
    if raw[:2] != b"\x1f\x8b":
        return raw
    try:
        return gzip.decompress(raw)
    except (OSError, EOFError) as e:
        raise TraceFormatError(cs.TRACE_ERR_BAD_PPROF.format(path=profile_path)) from e


def _decode_profile(
    raw: bytes,
) -> tuple[list[str], dict[int, _Function], dict[int, list[int]], list[_Sample]]:
    """The pprof message's string, function, location, and sample tables."""
    strings: list[str] = []
    functions: dict[int, _Function] = {}
    locations: dict[int, list[int]] = {}
    samples: list[_Sample] = []
    for field, _wire, value in _fields(raw):
        if not isinstance(value, bytes):
            continue
        if field == 2:
            samples.append(_parse_sample(value))
        elif field == 4:
            location_id, function_ids = _parse_location(value)
            locations[location_id] = function_ids
        elif field == 5:
            function_id, function = _parse_function(value)
            functions[function_id] = function
        elif field == 6:
            strings.append(value.decode("utf-8", errors="replace"))
    return strings, functions, locations, samples


def _build_frame(
    function: _Function | None, strings: list[str], root_prefix: str
) -> FramePoint | None:
    """A project FramePoint for the function, or None if out of scope."""
    if function is None or not (0 < function.filename_index < len(strings)):
        return None
    path = strings[function.filename_index]
    if not path.startswith(root_prefix):
        return None
    if _GO_EXCLUDED_DIR in path[len(root_prefix) :].split("/"):
        return None
    name = (
        strings[function.name_index] if 0 < function.name_index < len(strings) else ""
    )
    return FramePoint(
        path=path, qualname=_bare_name(name), line=max(function.start_line, 0)
    )


def _accumulate_edges(
    samples: list[_Sample],
    locations: dict[int, list[int]],
    resolve: Callable[[int], FramePoint | None],
) -> dict[tuple[FramePoint, FramePoint], int]:
    """Adjacent in-project frames across each leaf-first sampled stack.

    Samples are leaf-first; walk root-first, expanding inline frames
    outermost-first within each location.
    """
    edges: dict[tuple[FramePoint, FramePoint], int] = {}
    for location_ids, weight in samples:
        ancestor: FramePoint | None = None
        for location_id in reversed(location_ids):
            for function_id in reversed(locations.get(location_id, [])):
                frame = resolve(function_id)
                if frame is None:
                    continue
                if ancestor is not None:
                    key = (ancestor, frame)
                    edges[key] = edges.get(key, 0) + weight
                ancestor = frame
    return edges


def convert_pprof_profile(
    profile_path: Path,
    repo_root: Path,
    output: Path,
    workload: str | None,
    *,
    build_frame: Callable[[_Function | None, list[str], str], FramePoint | None],
    language: str,
    tracer: str,
) -> int:
    """Decode a pprof profile and write its project call edges to ``output``.

    Shared by the Go and Rust converters, which differ only in ``build_frame``
    (symbol demangling and out-of-scope directories) and the header tags. pprof
    profiles are sampled, so the header is always flagged ``sampled``.
    """
    raw = _decompress(profile_path.read_bytes(), profile_path)
    try:
        strings, functions, locations, samples = _decode_profile(raw)
    except TraceFormatError as e:
        raise TraceFormatError(cs.TRACE_ERR_BAD_PPROF.format(path=profile_path)) from e
    if not strings or not functions or not samples:
        raise TraceFormatError(cs.TRACE_ERR_BAD_PPROF.format(path=profile_path))

    root_prefix = repo_root.resolve().as_posix() + "/"
    frames: dict[int, FramePoint | None] = {}

    def _frame(function_id: int) -> FramePoint | None:
        if function_id not in frames:
            frames[function_id] = build_frame(
                functions.get(function_id), strings, root_prefix
            )
        return frames[function_id]

    edges = _accumulate_edges(samples, locations, _frame)

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
        language=language,
        repo_root=str(repo_root),
        tracer=tracer,
        sampled=True,
    )
    write_trace_file(output, header, records)
    return len(records)


def convert_pprof(
    profile_path: Path,
    repo_root: Path,
    output: Path,
    workload: str | None = None,
) -> int:
    """Write ``profile_path``'s project call edges to ``output``; returns count."""
    return convert_pprof_profile(
        profile_path,
        repo_root,
        output,
        workload,
        build_frame=_build_frame,
        language=cs.TRACE_LANGUAGE_GO,
        tracer=cs.TRACE_TOOL_NAME_PPROF,
    )
