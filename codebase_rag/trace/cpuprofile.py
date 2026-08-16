"""Convert V8 CPU profiles (``node --cpu-prof``) to the trace interchange format.

A ``.cpuprofile`` encodes every observed call stack as a tree: each node is a
function frame, each parent/child link a caller/callee relationship the
sampler actually saw. That makes the tree a genuine (if sampled) dynamic call
graph: edges through registries, event emitters, and dynamic ``import()`` are
present whenever a sample landed inside them. Counts are sample counts, not
call counts; they order edges by weight but do not enumerate invocations.

Runtime-internal frames (``node:``), files outside the repository, and
excluded directories such as ``node_modules`` are not project code; edges see
through them to the nearest project ancestor, mirroring the JVM agent's stack
walk, so ``list.forEach(callback)`` attributes the callback to the code that
scheduled it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse

from loguru import logger

from .. import constants as cs
from .records import (
    CallRecord,
    FramePoint,
    TraceFormatError,
    TraceHeader,
    write_trace_file,
)
from .sourcemap import SourceMapIndex, SourceMapOutcome


@dataclass(frozen=True, slots=True)
class _ProfileFrame:
    path: str
    qualname: str
    line: int


def _aggregate_project_edges(
    frames: dict[int, _ProfileFrame | None],
    children: dict[int, list[int]],
    subtree: dict[int, int],
    roots: list[int],
) -> dict[tuple[_ProfileFrame, _ProfileFrame], int]:
    """Weighted caller/callee pairs between project frames.

    Walks the forest keeping each node's nearest project ancestor, so
    runtime-internal frames between two project frames are seen through;
    each project node contributes its subtree's sample weight to the edge
    from that ancestor.
    """
    edges: dict[tuple[_ProfileFrame, _ProfileFrame], int] = {}
    stack: list[tuple[int, _ProfileFrame | None]] = [(root, None) for root in roots]
    while stack:
        node_id, ancestor = stack.pop()
        frame = frames[node_id]
        if frame is not None:
            if ancestor is not None:
                key = (ancestor, frame)
                edges[key] = edges.get(key, 0) + max(subtree[node_id], 1)
            ancestor = frame
        stack.extend((child, ancestor) for child in children[node_id])
    return edges


_DRIVE_LETTER = re.compile(r"^/[A-Za-z]:/")


def _url_to_path(url: str) -> str:
    """A ``file://`` URL as a native filesystem path.

    Windows URLs carry the drive letter after the root slash
    (``file:///C:/repo``); stripping that slash and normalising separators
    keeps project frames matchable against the repo prefix on any platform.
    """
    # V8 URLs (and Path.as_uri) percent-encode spaces and other characters;
    # decode so the path matches the real repo prefix.
    path = unquote(urlparse(url).path)
    if _DRIVE_LETTER.match(path):
        path = path[1:]
    # Normalise to POSIX separators so a Windows drive path (`C:\repo\main.js`)
    # matches the POSIX `root_prefix`; the graph stores POSIX paths too.
    return Path(path).as_posix()


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _project_frame(
    call_frame: dict[str, object],
    root_prefix: str,
    source_maps: SourceMapIndex,
    stats: Counter[SourceMapOutcome],
) -> _ProfileFrame | None:
    url = call_frame.get("url")
    if not isinstance(url, str) or not url.startswith(cs.TRACE_JS_FILE_URL_PREFIX):
        return None
    generated_path = _url_to_path(url)
    line = call_frame.get("lineNumber")
    if not _is_index(line):
        return None
    line = cast("int", line)
    name = call_frame.get("functionName")
    if not isinstance(name, str) or not name:
        # V8 reports module toplevels as a nameless frame at line 0; other
        # nameless frames are anonymous functions, resolvable only by span.
        # The generated line drives this even when a source map relocates it.
        name = cs.TRACE_QUALNAME_MODULE if line == 0 else cs.TRACE_QUALNAME_ANONYMOUS
    # A transpiled file (dist/*.js) with a source map relocates back to its
    # original TypeScript/JavaScript position, so the frame lands on the source
    # node the graph indexed; without a map the generated position stands.
    column = call_frame.get("columnNumber")
    if _is_index(column):
        remapped, outcome = source_maps.remap_detailed(
            generated_path, line, cast("int", column)
        )
    else:
        remapped, outcome = None, SourceMapOutcome.NO_MAP
    if remapped is not None:
        path, source_line = remapped
    else:
        path, source_line = generated_path, line + 1
    if not path.startswith(root_prefix):
        return None
    if not cs.TRACE_EXCLUDED_DIR_NAMES.isdisjoint(Path(path).parts):
        return None
    # Only project frames count toward the resolution rate; the source-map
    # outcome categorises whether each landed on its source or fell back.
    stats[outcome] += 1
    return _ProfileFrame(path=path, qualname=name, line=source_line)


def _int_ids(values: object) -> bool:
    """Whether ``values`` is a list of plain (non-bool) integers."""
    return isinstance(values, list) and all(
        isinstance(v, int) and not isinstance(v, bool) for v in values
    )


def _parse_nodes(
    nodes: list[object],
    root_prefix: str,
    profile_path: Path,
    source_maps: SourceMapIndex,
    stats: Counter[SourceMapOutcome],
) -> tuple[dict[int, _ProfileFrame | None], dict[int, list[int]], dict[int, int]]:
    """Validate each profile node and index frames, children, and hit counts."""
    frames: dict[int, _ProfileFrame | None] = {}
    children: dict[int, list[int]] = {}
    hits: dict[int, int] = {}
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            raise TraceFormatError(
                cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path)
            )
        node = cast("dict[str, object]", raw_node)
        node_id = node.get("id")
        call_frame = node.get("callFrame")
        raw_children = node.get("children", [])
        if (
            not isinstance(node_id, int)
            or isinstance(node_id, bool)
            or node_id in children
            or not isinstance(call_frame, dict)
            or not _int_ids(raw_children)
        ):
            raise TraceFormatError(
                cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path)
            )
        frames[node_id] = _project_frame(
            cast("dict[str, object]", call_frame), root_prefix, source_maps, stats
        )
        children[node_id] = list(cast("list[int]", raw_children))
        hit_count = node.get("hitCount", 0)
        hits[node_id] = hit_count if isinstance(hit_count, int) else 0
    return frames, children, hits


def _report_resolution(stats: Counter[SourceMapOutcome]) -> None:
    """Log the source-map resolution rate and categorise any failures."""
    total = sum(stats.values())
    if not total:
        return
    resolved = stats[SourceMapOutcome.RESOLVED]
    logger.info(
        cs.TRACE_MSG_SOURCEMAP_RESOLUTION.format(
            resolved=resolved, total=total, rate=f"{resolved / total:.0%}"
        )
    )
    for outcome in SourceMapOutcome:
        count = stats[outcome]
        if outcome is not SourceMapOutcome.RESOLVED and count:
            logger.info(
                cs.TRACE_MSG_SOURCEMAP_DETAIL.format(outcome=outcome.value, count=count)
            )


def convert_cpuprofile(
    profile_path: Path,
    repo_root: Path,
    output: Path,
    workload: str | None = None,
) -> int:
    """Write ``profile_path``'s project call edges to ``output``; returns count."""
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TraceFormatError(
            cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path)
        ) from e
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise TraceFormatError(cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path))

    root_prefix = repo_root.resolve().as_posix() + "/"
    resolution: Counter[SourceMapOutcome] = Counter()
    frames, children, hits = _parse_nodes(
        nodes, root_prefix, profile_path, SourceMapIndex(), resolution
    )
    _report_resolution(resolution)

    # A well-formed profile is a forest: every child id exists and has
    # exactly one parent. Anything else (dangling ids, shared children,
    # cycles) is a malformed profile, not a crash.
    all_children = [c for kids in children.values() for c in kids]
    child_ids = set(all_children)
    if len(all_children) != len(child_ids) or not child_ids.issubset(frames):
        raise TraceFormatError(cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path))
    roots = [node_id for node_id in frames if node_id not in child_ids]

    # Total samples in each subtree: children first, then parents, without
    # recursing (V8 stacks can be deeper than the interpreter allows).
    order: list[int] = []
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        order.append(node_id)
        stack.extend(children[node_id])
    if len(order) != len(frames):
        # Nodes unreachable from any root can only mean a cycle.
        raise TraceFormatError(cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path))
    subtree: dict[int, int] = {}
    for node_id in reversed(order):
        subtree[node_id] = hits[node_id] + sum(subtree[c] for c in children[node_id])

    edges = _aggregate_project_edges(frames, children, subtree, roots)

    workloads = (workload,) if workload else ()
    records = [
        CallRecord(
            caller=FramePoint(
                path=caller.path, qualname=caller.qualname, line=caller.line
            ),
            callee=FramePoint(
                path=callee.path, qualname=callee.qualname, line=callee.line
            ),
            count=count,
            workloads=workloads,
            receiver_types=(),
        )
        for (caller, callee), count in edges.items()
    ]
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=cs.TRACE_LANGUAGE_JS,
        repo_root=str(repo_root),
        tracer=cs.TRACE_TOOL_NAME_CPUPROFILE,
        sampled=True,
    )
    write_trace_file(output, header, records)
    return len(records)
