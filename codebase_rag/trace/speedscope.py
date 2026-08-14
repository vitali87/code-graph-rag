"""Convert dotnet-trace speedscope exports to the trace interchange format.

``dotnet-trace collect`` samples managed stacks over EventPipe;
``dotnet-trace convert --format speedscope`` exports them as speedscope JSON
whose samples are full stacks, root first. Adjacent in-scope frames within a
stack are caller/callee relationships the sampler actually observed, so
dispatch through interfaces, DI containers, and reflection appears whenever
samples landed there. Counts are sample weights, not call counts.

.NET frames carry no source paths or lines, only
``Assembly!Namespace.Class.Method(arg types)``. Scoping therefore works on
name prefixes (the ``include`` namespaces) rather than file paths, and
frames from runtime assemblies are walked through to the nearest in-scope
ancestor, mirroring the JVM agent's stack walk.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, TypeGuard, cast

from .. import constants as cs
from .records import (
    CallRecord,
    FramePoint,
    TraceFormatError,
    TraceHeader,
    write_trace_file,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _scoped_name(name: object, include: Sequence[str]) -> str | None:
    """The frame's assembly-stripped, argument-stripped dotted name, if in scope."""
    if not isinstance(name, str):
        return None
    _assembly, separator, rest = name.partition(cs.TRACE_DOTNET_ASSEMBLY_SEPARATOR)
    if not separator:
        return None
    args_start = rest.find("(")
    if args_start >= 0:
        rest = rest[:args_start]
    for prefix in include:
        if rest == prefix or rest.startswith(prefix + cs.SEPARATOR_DOT):
            return rest
    return None


def _valid_index(value: object, count: int) -> TypeGuard[int]:
    """A plain (non-bool) int that indexes the frame table."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < count


def _sample_weight(weights: list[object], position: int) -> float:
    """The sample's weight, defaulting non-positive or non-numeric values to 1."""
    weight = weights[position] if position < len(weights) else 1
    if (
        not isinstance(weight, int | float)
        or isinstance(weight, bool)
        or not math.isfinite(weight)
        or weight <= 0
    ):
        return 1.0
    return float(weight)


def _accumulate_sampled(
    profile: dict[str, object],
    names: list[str | None],
    edges: dict[tuple[str, str], float],
) -> bool:
    """Adjacent in-scope frames in each sampled stack, weighted by sample."""
    samples = profile.get("samples", [])
    weights = profile.get("weights", [])
    if not isinstance(samples, list) or not isinstance(weights, list):
        return False
    for position, stack in enumerate(samples):
        if not isinstance(stack, list):
            return False
        weight = _sample_weight(cast("list[object]", weights), position)
        ancestor: str | None = None
        for frame_index in stack:
            if not _valid_index(frame_index, len(names)):
                return False
            current = names[frame_index]
            if current is None:
                continue
            if ancestor is not None:
                key = (ancestor, current)
                edges[key] = edges.get(key, 0) + weight
            ancestor = current
    return True


def _open_event(
    event: dict[str, object],
    names: list[str | None],
    stack: list[str | None],
    edges: dict[tuple[str, str], float],
) -> bool:
    """Replay one ``O`` event: validate the frame and count the in-scope edge."""
    frame_index = event.get("frame")
    if not _valid_index(frame_index, len(names)):
        return False
    current = names[frame_index]
    if current is not None:
        ancestor = next((name for name in reversed(stack) if name is not None), None)
        if ancestor is not None:
            key = (ancestor, current)
            edges[key] = edges.get(key, 0) + 1
    stack.append(current)
    return True


def _accumulate_evented(
    profile: dict[str, object],
    names: list[str | None],
    edges: dict[tuple[str, str], float],
) -> bool:
    """Frame open/close events replayed as a stack; each in-scope open under
    an in-scope ancestor counts one observed activation."""
    events = profile.get("events")
    if not isinstance(events, list):
        return False
    stack: list[str | None] = []
    for raw_event in cast("list[object]", events):
        if not isinstance(raw_event, dict):
            return False
        event = cast("dict[str, object]", raw_event)
        kind = event.get("type")
        if kind == "O":
            if not _open_event(event, names, stack, edges):
                return False
        elif kind == "C":
            if not stack:
                return False
            stack.pop()
        else:
            return False
    return True


def _accumulate_profiles(
    profiles: list[object],
    names: list[str | None],
    edges: dict[tuple[str, str], float],
    profile_path: Path,
) -> None:
    """Accumulate every recognised profile; raise if none was usable."""
    converted = False
    for raw_profile in profiles:
        if not isinstance(raw_profile, dict):
            continue
        profile = cast("dict[str, object]", raw_profile)
        kind = profile.get("type")
        if kind == "sampled":
            ok = _accumulate_sampled(profile, names, edges)
        elif kind == "evented":
            ok = _accumulate_evented(profile, names, edges)
        else:
            continue
        if not ok:
            raise TraceFormatError(
                cs.TRACE_ERR_BAD_SPEEDSCOPE.format(path=profile_path)
            )
        converted = True
    if not converted:
        raise TraceFormatError(cs.TRACE_ERR_BAD_SPEEDSCOPE.format(path=profile_path))


def convert_speedscope(
    profile_path: Path,
    output: Path,
    include: Sequence[str],
    workload: str | None = None,
) -> int:
    """Write ``profile_path``'s in-scope call edges to ``output``; returns count."""
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    shared = raw.get("shared") if isinstance(raw, dict) else None
    frames = shared.get("frames") if isinstance(shared, dict) else None
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(frames, list) or not isinstance(profiles, list):
        raise TraceFormatError(cs.TRACE_ERR_BAD_SPEEDSCOPE.format(path=profile_path))

    names: list[str | None] = [
        _scoped_name(f.get("name") if isinstance(f, dict) else None, include)
        for f in frames
    ]

    # Fractional sample weights accumulate as-is and round half-up only at
    # emission, so their combined contribution is never truncated away.
    edges: dict[tuple[str, str], float] = {}
    _accumulate_profiles(profiles, names, edges, profile_path)

    workloads = (workload,) if workload else ()
    records = [
        CallRecord(
            caller=FramePoint(path="", qualname=caller, line=0),
            callee=FramePoint(path="", qualname=callee, line=0),
            count=max(int(count + 0.5), 1),
            workloads=workloads,
            receiver_types=(),
        )
        for (caller, callee), count in edges.items()
    ]
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_DOTNET,
        repo_root="",
        tracer=cs.TRACE_TOOL_NAME_SPEEDSCOPE,
    )
    write_trace_file(output, header, records)
    return len(records)
