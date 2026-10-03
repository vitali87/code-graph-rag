"""Public facade for extract and inline refactoring operations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..graph_query import QueryFn
from .contract import Reingest
from .extract_types import ExtractRefused, ExtractReport, InlineRefused, InlineReport
from .transaction import StagedTree, VerificationResult

__all__ = [
    "ExtractRefused",
    "ExtractReport",
    "InlineRefused",
    "InlineReport",
    "extract",
    "inline",
]


def extract(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    span: tuple[int, int],
    new_name: str,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> ExtractReport:
    from .extract_operation import Extractor

    extractor = Extractor(
        repo_root, fetch_all, project_name, verify=verify, reingest=reingest
    )
    if dry_run:
        report, _patcher = extractor.plan(qualified_name, span, new_name)
        return report
    return extractor.apply(qualified_name, span, new_name)


def inline(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> InlineReport:
    from .inline_operation import Inliner

    inliner = Inliner(
        repo_root, fetch_all, project_name, verify=verify, reingest=reingest
    )
    if dry_run:
        report, _patcher = inliner.plan(qualified_name)
        return report
    return inliner.apply(qualified_name)
