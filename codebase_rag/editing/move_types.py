"""Public report and refusal types for the move refactoring."""

from __future__ import annotations

from typing import NamedTuple

from .contract import Verdict


class MoveRefused(ValueError):
    """The move cannot be planned as asked; nothing was written."""

    def __init__(self, message: str, cycle: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.cycle = cycle


class MoveReport(NamedTuple):
    qualified_name: str
    new_qualified_name: str
    old_path: str
    new_path: str
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    importers: tuple[str, ...]
    unchanged_importers: tuple[str, ...]
    copied_imports: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None


class _Cut(NamedTuple):
    start: int
    end: int
    text: str


class _NeededImport(NamedTuple):
    statement: str
    target_qn: str
