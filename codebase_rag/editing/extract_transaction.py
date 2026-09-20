"""Transaction and postcondition helpers for extract/inline edits."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..graph_query import QueryFn
from .contract import Expectation, Reingest, measure, verify
from .patcher import Patcher
from .transaction import EditTransaction, StagedTree, VerificationResult, undo_last


def _commit(
    patcher: Patcher,
    repo_root: Path,
    verifier: Callable[[StagedTree], VerificationResult | bool | None] | None,
):
    tx = EditTransaction(repo_root)
    results = patcher.stage_into(tx)
    broken = [key for key, result in results.items() if result.parses is False]
    if broken:
        tx.rollback()
        return None, broken

    def check(tree: StagedTree) -> VerificationResult | bool | None:
        return verifier(tree) if verifier is not None else True

    return tx.commit(check), []


def _enforce(
    report: Any,
    expectation: Expectation,
    fetch_all: QueryFn,
    project: str,
    repo_root: Path,
    reingest: Reingest,
    failure: str,
):
    delta = measure(fetch_all, project, repo_root, report.files, reingest)
    verdict = verify(expectation, delta)
    if verdict.ok:
        return report._replace(verdict=verdict)
    undo_last(repo_root)
    reingest(list(report.files))
    return report._replace(
        applied=False,
        verdict=verdict,
        message=failure.format(reasons="; ".join(verdict.failures)),
    )
