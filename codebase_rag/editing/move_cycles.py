"""Import-cycle refusal for move planning."""

from __future__ import annotations

from .. import constants as cs
from ..graph_query import QueryFn
from ..structural_delta import import_cycles, snapshot
from .move_types import MoveRefused


class _MoveCycleMixin:
    fetch_all: QueryFn
    project: str

    def _refuse_cycles(
        self,
        old_module: str,
        new_module: str,
        copied_targets: list[str],
        needs_old: bool,
        old_needs_new: bool,
        old_path: str,
        new_path: str,
    ) -> None:
        graph = {
            qn: set(targets)
            for qn, targets in snapshot(
                self.fetch_all, self.project, [old_path, new_path]
            ).imports.items()
        }
        before = import_cycles({qn: frozenset(t) for qn, t in graph.items()})
        graph.setdefault(new_module, set()).update(t for t in copied_targets if t)
        if needs_old:
            graph.setdefault(new_module, set()).add(old_module)
        if old_needs_new:
            graph.setdefault(old_module, set()).add(new_module)
        for importer, targets in graph.items():
            if old_module in targets and importer not in (old_module, new_module):
                targets.add(new_module)
        after = import_cycles({qn: frozenset(t) for qn, t in graph.items()})
        fresh = [c for c in after - before if new_module in c or old_module in c]
        if fresh:
            cycle = tuple(sorted(fresh[0]))
            raise MoveRefused(cs.MOVE_CYCLE.format(cycle=" -> ".join(cycle)), cycle)
