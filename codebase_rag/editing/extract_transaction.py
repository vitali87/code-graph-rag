"""Transaction and postcondition helpers for extract/inline edits."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from .. import constants as cs
from ..graph_query import QueryFn
from ..graph_updater import ReingestAborted
from .contract import Expectation, Reingest, Verdict, measure, verify
from .patcher import Patcher
from .transaction import (
    EditTransaction,
    StagedTree,
    TransactionConflict,
    TransactionOutcome,
    VerificationResult,
    undo_transaction,
)


def _commit(
    patcher: Patcher,
    repo_root: Path,
    verifier: Callable[[StagedTree], VerificationResult | bool | None] | None,
) -> tuple[TransactionOutcome | None, list[str]]:
    tx = EditTransaction(repo_root)
    results = patcher.stage_into(tx)
    broken = [key for key, result in results.items() if result.parses is False]
    if broken:
        tx.rollback()
        return None, broken

    def check(tree: StagedTree) -> VerificationResult | bool | None:
        return verifier(tree) if verifier is not None else True

    return tx.commit(check), []


class Enforced(NamedTuple):
    """What the postcondition decided for an applied extract or inline."""

    applied: bool
    message: str
    verdict: Verdict | None
    graph_incomplete: bool = False


def _enforce(
    files: tuple[str, ...],
    transaction_id: str,
    message: str,
    expectation: Expectation,
    fetch_all: QueryFn,
    project: str,
    repo_root: Path,
    reingest: Reingest,
    failure: str,
) -> Enforced:
    try:
        delta = measure(fetch_all, project, repo_root, files, reingest)
    # The edit has landed; a graph that cannot be measured is reported, never
    # raised past the committed edit (as for change_signature and move).
    except Exception as error:  # noqa: BLE001
        unmeasured = cs.EDIT_CONTRACT_UNMEASURED.format(error=error)
        logger.warning(unmeasured)
        # `ValueError` and `ReingestAborted` are raised before the re-ingest
        # writes anything, so the graph is still whole.
        return Enforced(
            True,
            unmeasured,
            None,
            graph_incomplete=not isinstance(error, ValueError | ReingestAborted),
        )
    verdict = verify(expectation, delta)
    if verdict.ok:
        return Enforced(True, message, verdict)
    reasons = "; ".join(verdict.failures)
    # This report's own transaction, never whatever is newest: `undo_last`
    # reversed an unrelated later edit and left this one on disk while the
    # report claimed a rollback (Greptile and Copilot, PR #2057). A later
    # edit stacked on it, or a hand edit to its files, refuses the undo.
    try:
        outcome = undo_transaction(repo_root, transaction_id)
    except TransactionConflict as conflict:
        logger.warning(str(conflict))
        return Enforced(
            False,
            cs.EDIT_ROLLBACK_REFUSED.format(reasons=reasons, error=conflict),
            verdict,
        )
    if not outcome.applied:
        return Enforced(
            False,
            cs.EDIT_ROLLBACK_REFUSED.format(reasons=reasons, error=outcome.message),
            verdict,
        )
    # Only a confirmed undo changed the tree, so only then does the graph
    # need re-describing.
    try:
        reingest(list(files))
    # The files are restored; the graph may have lost the subtree the
    # re-ingest deleted before failing. Say so, never raise.
    except Exception as error:  # noqa: BLE001
        unmeasured = cs.EDIT_ROLLBACK_UNMEASURED.format(reasons=reasons, error=error)
        logger.warning(unmeasured)
        return Enforced(False, unmeasured, verdict, graph_incomplete=True)
    return Enforced(False, failure.format(reasons=reasons), verdict)
