"""Transaction and postcondition helpers for extract/inline edits."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from .. import constants as cs
from ..graph_query import QueryFn
from .contract import Expectation, Reingest, measure, verify
from .patcher import Patcher
from .transaction import (
    EditTransaction,
    StagedTree,
    TransactionConflict,
    VerificationResult,
    undo_transaction,
)


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
    reasons = "; ".join(verdict.failures)
    report = report._replace(applied=False, verdict=verdict)
    # This report's own transaction, never whatever is newest: `undo_last`
    # reversed an unrelated later edit and left this one on disk while the
    # report claimed a rollback (Greptile and Copilot, PR #2057). A later
    # edit stacked on it, or a hand edit to its files, refuses the undo.
    try:
        outcome = undo_transaction(repo_root, report.transaction_id)
    except TransactionConflict as conflict:
        logger.warning(str(conflict))
        return report._replace(
            message=cs.EDIT_ROLLBACK_REFUSED.format(reasons=reasons, error=conflict)
        )
    if not outcome.applied:
        return report._replace(
            message=cs.EDIT_ROLLBACK_REFUSED.format(
                reasons=reasons, error=outcome.message
            )
        )
    # Only a confirmed undo changed the tree, so only then does the graph
    # need re-describing.
    reingest(list(report.files))
    return report._replace(message=failure.format(reasons=reasons))
