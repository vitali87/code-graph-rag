"""Resolution of runtime frames to graph nodes.

A traced frame is identified by ``(absolute path, co_qualname, first line)``;
graph callables are identified by qualified name. The mapping strips runtime
artifacts (``<locals>`` scopes, ``@line`` duplicate markers) and falls back to
line containment when names alone are ambiguous.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from .records import FramePoint


@dataclass(frozen=True, slots=True)
class CallableNode:
    """A graph node a traced frame may resolve to."""

    label: str
    qualified_name: str
    path: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True, slots=True)
class ResolvedFrame:
    label: str
    qualified_name: str


@dataclass(slots=True)
class ResolutionStats:
    """Counts of unresolvable frames, keyed by reason."""

    unresolved: dict[str, int] = field(default_factory=dict)

    def record(self, reason: cs.TraceUnresolvedReason) -> None:
        self.unresolved[reason.value] = self.unresolved.get(reason.value, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.unresolved.values())


def _natural_qualified_name(qualified_name: str) -> str:
    """Strip the duplicate-definition marker (``qn@line`` or ``qn@line_col``)."""
    base, _, suffix = qualified_name.rpartition(cs.DUP_QN_MARKER)
    if not base:
        return qualified_name
    plain = suffix.replace(cs.DUP_QN_COLUMN_MARKER, "")
    return base if plain.isdigit() else qualified_name


class FrameResolver:
    """Maps runtime frame identities of one project to graph nodes."""

    def __init__(self, repo_root: Path, nodes: list[CallableNode]) -> None:
        self._repo_root = repo_root.resolve()
        self._root_prefix = str(self._repo_root) + os.sep
        self._callables_by_path: dict[str, list[CallableNode]] = {}
        self._modules_by_path: dict[str, CallableNode] = {}
        for node in nodes:
            if node.label == cs.NodeLabel.MODULE:
                self._modules_by_path[node.path] = node
            else:
                self._callables_by_path.setdefault(node.path, []).append(node)

    def resolve(
        self, frame: FramePoint, stats: ResolutionStats
    ) -> ResolvedFrame | None:
        if not frame.path.startswith(self._root_prefix):
            stats.record(cs.TraceUnresolvedReason.OUTSIDE_REPO)
            return None
        rel_path = Path(frame.path).relative_to(self._repo_root).as_posix()

        parts = [
            p
            for p in frame.qualname.split(cs.SEPARATOR_DOT)
            if p != cs.TRACE_QUALNAME_LOCALS
        ]
        if parts == [cs.TRACE_QUALNAME_MODULE]:
            module = self._modules_by_path.get(rel_path)
            if module is None:
                stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
                return None
            return ResolvedFrame(
                label=module.label, qualified_name=module.qualified_name
            )
        if any(p.startswith(cs.TRACE_SYNTHETIC_PREFIX) for p in parts):
            stats.record(cs.TraceUnresolvedReason.SYNTHETIC)
            return None

        candidates = self._callables_by_path.get(rel_path)
        if not candidates:
            stats.record(cs.TraceUnresolvedReason.UNKNOWN_PATH)
            return None

        suffix = cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
        by_name = [
            n
            for n in candidates
            if _natural_qualified_name(n.qualified_name).endswith(suffix)
        ]
        # Prefer a name match whose span contains the runtime line; among name
        # matches without span data, take the first by qualified name so
        # resolution stays deterministic. Only when no candidate matches by
        # name does the line span alone decide.
        chosen = (
            self._innermost_span_containing_line(by_name, frame.line)
            or (min(by_name, key=lambda n: n.qualified_name) if by_name else None)
            or self._innermost_span_containing_line(candidates, frame.line)
        )
        if chosen is None:
            stats.record(cs.TraceUnresolvedReason.NO_MATCH)
            return None
        return ResolvedFrame(label=chosen.label, qualified_name=chosen.qualified_name)

    @staticmethod
    def _innermost_span_containing_line(
        candidates: list[CallableNode], line: int
    ) -> CallableNode | None:
        containing = [
            n
            for n in candidates
            if n.start_line is not None
            and n.end_line is not None
            and n.start_line <= line <= n.end_line
        ]
        if not containing:
            return None
        # The innermost span wins: a nested function's span sits inside
        # its parent's, and the runtime line points at the inner def.
        return max(containing, key=lambda n: n.start_line or 0)
