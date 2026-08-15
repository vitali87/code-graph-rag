"""Runtime call-graph tracer for Python built on ``sys.monitoring`` (PEP 669).

Records caller/callee pairs for Python-to-Python calls whose code lives under a
repository root, aggregating in memory and flushing to the trace interchange
format. Receiver types are sampled for bound methods so dynamic dispatch can be
resolved to the concrete implementation that ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs
from .records import CallRecord, FramePoint, TraceHeader, write_trace_file

if TYPE_CHECKING:
    from types import CodeType

import os
import sys

_NO_WORKLOAD = -1


@dataclass(slots=True)
class _PairStats:
    count: int = 0
    workloads: set[int] = field(default_factory=set)
    receiver_types: set[str] = field(default_factory=set)


class CallGraphTracer:
    """Aggregating tracer scoped to files under ``repo_root``.

    Not reentrant: one instance may be started at a time per interpreter, since
    ``sys.monitoring`` allows a single profiler tool registration.

    Call counts and workload attribution are best-effort under threads:
    ``sys.monitoring`` callbacks fire on every thread, aggregation is unlocked
    (locking the hot path is not worth it for provenance metadata), and the
    current workload is interpreter-wide, so calls made by background threads
    are attributed to the workload the main thread set last.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root.resolve()
        # Prefix comparison against co_filename must include the separator so
        # a sibling directory like /repo-extras does not match /repo.
        self._root_prefix = str(self._repo_root) + os.sep
        self._pairs: dict[tuple[CodeType, CodeType], _PairStats] = {}
        self._scope_cache: dict[str, bool] = {}
        self._workloads: list[str] = []
        self._workload_ids: dict[str, int] = {}
        self._current_workload = _NO_WORKLOAD
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def set_workload(self, workload: str | None) -> None:
        if workload is None:
            self._current_workload = _NO_WORKLOAD
            return
        known = self._workload_ids.get(workload)
        if known is None:
            known = len(self._workloads)
            self._workloads.append(workload)
            self._workload_ids[workload] = known
        self._current_workload = known

    def start(self) -> None:
        monitoring = sys.monitoring
        monitoring.use_tool_id(monitoring.PROFILER_ID, cs.TRACE_TOOL_NAME)
        try:
            monitoring.register_callback(
                monitoring.PROFILER_ID, monitoring.events.PY_START, self._on_py_start
            )
            monitoring.set_events(monitoring.PROFILER_ID, monitoring.events.PY_START)
        except BaseException:
            # Without this, a failed setup leaves the profiler slot claimed
            # while stop() skips cleanup because _active stayed False.
            monitoring.free_tool_id(monitoring.PROFILER_ID)
            raise
        self._active = True

    def stop(self) -> None:
        if not self._active:
            return
        monitoring = sys.monitoring
        monitoring.set_events(monitoring.PROFILER_ID, 0)
        monitoring.register_callback(
            monitoring.PROFILER_ID, monitoring.events.PY_START, None
        )
        monitoring.free_tool_id(monitoring.PROFILER_ID)
        self._active = False

    def _in_scope(self, filename: str) -> bool:
        cached = self._scope_cache.get(filename)
        if cached is None:
            cached = filename.startswith(
                self._root_prefix
            ) and cs.TRACE_EXCLUDED_DIR_NAMES.isdisjoint(Path(filename).parts)
            self._scope_cache[filename] = cached
        return cached

    def _on_py_start(self, code: CodeType, instruction_offset: int) -> None:
        if not self._in_scope(code.co_filename):
            return
        # The callback runs in its own frame; f_back is the frame that just
        # started executing `code`, and its f_back is the caller.
        callee_frame = sys._getframe(1)
        caller_frame = callee_frame.f_back
        if caller_frame is None:
            return
        caller_code = caller_frame.f_code
        if not self._in_scope(caller_code.co_filename):
            return
        stats = self._pairs.get((caller_code, code))
        if stats is None:
            stats = _PairStats()
            self._pairs[(caller_code, code)] = stats
        stats.count += 1
        if self._current_workload != _NO_WORKLOAD:
            stats.workloads.add(self._current_workload)
        # Materialising f_locals is comparatively expensive, so receivers are
        # sampled only for a pair's first few observations.
        if (
            stats.count <= cs.TRACE_RECEIVER_SAMPLE_LIMIT
            and code.co_argcount >= 1
            and code.co_varnames[0] in cs.TRACE_RECEIVER_PARAMS
        ):
            receiver = callee_frame.f_locals.get(code.co_varnames[0])
            if receiver is not None:
                receiver_type = (
                    receiver if isinstance(receiver, type) else type(receiver)
                )
                stats.receiver_types.add(
                    f"{receiver_type.__module__}.{receiver_type.__qualname__}"
                )

    def _frame_point(self, code: CodeType) -> FramePoint:
        return FramePoint(
            path=code.co_filename,
            qualname=code.co_qualname,
            line=code.co_firstlineno,
        )

    def records(self) -> list[CallRecord]:
        result: list[CallRecord] = []
        for (caller_code, callee_code), stats in self._pairs.items():
            workloads = tuple(sorted(self._workloads[i] for i in stats.workloads))
            result.append(
                CallRecord(
                    caller=self._frame_point(caller_code),
                    callee=self._frame_point(callee_code),
                    count=stats.count,
                    workloads=workloads,
                    receiver_types=tuple(sorted(stats.receiver_types)),
                )
            )
        return result

    def write(self, output: Path) -> int:
        """Flush aggregated observations to ``output``; returns record count."""
        records = self.records()
        header = TraceHeader(
            version=cs.TRACE_FORMAT_VERSION,
            language=cs.TRACE_LANGUAGE_PYTHON,
            repo_root=str(self._repo_root),
            tracer=cs.TRACE_TOOL_NAME,
            sampled=False,
        )
        write_trace_file(output, header, records)
        return len(records)
