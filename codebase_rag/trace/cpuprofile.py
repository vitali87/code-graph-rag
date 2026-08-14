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
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

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


@dataclass(frozen=True, slots=True)
class _ProfileFrame:
    path: str
    qualname: str
    line: int


def _project_frame(
    call_frame: dict[str, object], root_prefix: str
) -> _ProfileFrame | None:
    url = call_frame.get("url")
    if not isinstance(url, str) or not url.startswith(cs.TRACE_JS_FILE_URL_PREFIX):
        return None
    path = urlparse(url).path
    if not path.startswith(root_prefix):
        return None
    if not cs.TRACE_EXCLUDED_DIR_NAMES.isdisjoint(path.split("/")):
        return None
    line = call_frame.get("lineNumber")
    if not isinstance(line, int) or isinstance(line, bool) or line < 0:
        return None
    name = call_frame.get("functionName")
    if not isinstance(name, str) or not name:
        # V8 reports module toplevels as a nameless frame at line 0; other
        # nameless frames are anonymous functions, resolvable only by span.
        name = cs.TRACE_QUALNAME_MODULE if line == 0 else cs.TRACE_QUALNAME_ANONYMOUS
    return _ProfileFrame(path=path, qualname=name, line=line + 1)


def convert_cpuprofile(
    profile_path: Path,
    repo_root: Path,
    output: Path,
    workload: str | None = None,
) -> int:
    """Write ``profile_path``'s project call edges to ``output``; returns count."""
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise TraceFormatError(cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path))

    root_prefix = repo_root.resolve().as_posix() + "/"
    frames: dict[int, _ProfileFrame | None] = {}
    children: dict[int, list[int]] = {}
    hits: dict[int, int] = {}
    for node in nodes:
        node_id = node.get("id")
        call_frame = node.get("callFrame")
        if not isinstance(node_id, int) or not isinstance(call_frame, dict):
            raise TraceFormatError(
                cs.TRACE_ERR_BAD_CPUPROFILE.format(path=profile_path)
            )
        frames[node_id] = _project_frame(call_frame, root_prefix)
        raw_children = node.get("children", [])
        children[node_id] = [c for c in raw_children if isinstance(c, int)]
        hit_count = node.get("hitCount", 0)
        hits[node_id] = hit_count if isinstance(hit_count, int) else 0

    child_ids = {c for kids in children.values() for c in kids}
    roots = [node_id for node_id in frames if node_id not in child_ids]

    # Total samples in each subtree, bottom-up over the (acyclic) tree.
    subtree: dict[int, int] = {}

    def _subtree(node_id: int) -> int:
        cached = subtree.get(node_id)
        if cached is None:
            cached = hits[node_id] + sum(_subtree(c) for c in children[node_id])
            subtree[node_id] = cached
        return cached

    edges: dict[tuple[_ProfileFrame, _ProfileFrame], int] = {}

    def _walk(node_id: int, ancestor: _ProfileFrame | None) -> None:
        frame = frames[node_id]
        if frame is not None:
            if ancestor is not None:
                key = (ancestor, frame)
                edges[key] = edges.get(key, 0) + max(_subtree(node_id), 1)
            ancestor = frame
        for child in children[node_id]:
            _walk(child, ancestor)

    for root in roots:
        _walk(root, None)

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
    )
    write_trace_file(output, header, records)
    return len(records)
