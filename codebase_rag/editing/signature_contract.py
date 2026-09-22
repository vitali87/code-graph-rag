"""The postcondition contract of an applied `change_signature` (issues #1531,
#1533): measure the change through the structural delta and undo it when a
site reads as neither mapped nor listed. Split from `signature.py`.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from .. import constants as cs
from ..graph_query import QueryFn
from ..graph_updater import ReingestAborted
from .contract import Reingest, change_signature_expectation, measure, verify
from .signature_spec import _CALL, SignatureReport
from .transaction import TransactionConflict, undo_transaction


def _site_key(path: str, line: int | None, col: int | None) -> str:
    """The identity the contract checks a call under: two calls can share a line."""
    return cs.CHAR_COLON.join((path, str(line), str(col)))


def enforce_contract(
    report: SignatureReport,
    allow_heuristic: bool,
    *,
    fetch_all: QueryFn,
    project: str,
    repo_root: Path,
    reingest: Reingest,
) -> SignatureReport:
    """Measure an applied change and undo it when its postcondition fails."""
    try:
        delta = measure(
            fetch_all,
            project,
            repo_root,
            report.files,
            reingest,
        )
    # The transaction has landed; a graph that cannot be measured is
    # reported, never raised past the committed edit.
    except Exception as error:  # noqa: BLE001
        logger.warning(cs.SIGNATURE_CONTRACT_UNMEASURED.format(error=error))
        return report._replace(
            verdict=None,
            # `reingest` rejects bad paths with `ValueError` and converts
            # every prologue failure to `ReingestAborted` before writing,
            # so those two mean the graph was never touched (the rule
            # `rename` and the guarded MCP callback apply).
            graph_incomplete=not isinstance(error, ValueError | ReingestAborted),
            message=cs.SIGNATURE_CONTRACT_UNMEASURED.format(error=error),
        )
    verdict = verify(
        change_signature_expectation(
            [
                _site_key(site.path, site.line, site.col)
                for site in report.unmapped
                if site.line is not None
            ],
            heuristic_allowed=allow_heuristic,
        ),
        delta,
        rewritten=[
            (_site_key(site.path, site.line, site.col), site.resolution)
            for site in report.sites
            if site.kind == _CALL
        ],
    )
    if verdict.ok:
        return report._replace(verdict=verdict)
    reasons = cs.SEPARATOR_SEMICOLON_SPACE.join(verdict.failures)
    try:
        # This change's own transaction, not whatever is newest: a later
        # edit stacked on it refuses the rollback instead.
        undo_transaction(repo_root, report.transaction_id)
    except TransactionConflict as conflict:
        logger.warning(str(conflict))
        return report._replace(
            verdict=verdict,
            message=cs.SIGNATURE_ROLLBACK_REFUSED.format(reasons=reasons),
        )
    try:
        reingest(list(report.files))
    # The files are restored; the graph may have lost the subtree the
    # re-ingest deleted before failing. Say so, never raise.
    except Exception as error:  # noqa: BLE001
        logger.warning(
            cs.SIGNATURE_ROLLBACK_UNMEASURED.format(reasons=reasons, error=error)
        )
        return report._replace(
            applied=False,
            verdict=verdict,
            graph_incomplete=True,
            message=cs.SIGNATURE_ROLLBACK_UNMEASURED.format(
                reasons=reasons, error=error
            ),
        )
    return report._replace(
        applied=False,
        verdict=verdict,
        message=cs.SIGNATURE_CONTRACT_FAILED.format(reasons=reasons),
    )
