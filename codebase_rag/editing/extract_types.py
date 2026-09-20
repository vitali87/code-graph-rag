"""Shared report types and refusal exceptions for extract/inline edits."""

from __future__ import annotations

from typing import NamedTuple

from .contract import Verdict


class ExtractRefused(ValueError):
    """The span cannot be extracted as asked; nothing was written."""


class InlineRefused(ValueError):
    """The function cannot be inlined as asked; nothing was written."""

    def __init__(self, message: str, sites: list[str] | None = None) -> None:
        super().__init__(message)
        self.sites = sites or []


class ExtractReport(NamedTuple):
    qualified_name: str
    new_qualified_name: str
    path: str
    span: tuple[int, int]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None


class InlineReport(NamedTuple):
    qualified_name: str
    sites: tuple[str, ...]
    definition_removed: bool
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None
