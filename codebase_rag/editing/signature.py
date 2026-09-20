"""`change_signature(qn, new_params)`: edit-algebra operation 2 (issue #1533).

The definition changes, its callers in other packages do not: that is the
most-cited cross-package breakage. The graph knows every call site with
its argument shape (issue #1522), how each edge was resolved (issue #1526)
and the declared parameter types (issue #1527), so every site can be
rewritten per an explicit mapping or listed as unmapped.

`new_params` describes the new parameter list in order. Each entry says
where its value comes from at a call site: an old positional index
(`from_index`, receiver excluded), an old parameter name (`from_name`),
a default literal (`literal`, inserted where the site passes nothing), or
nothing at all (unmapped: sites that pass no value are left untouched and
listed). Every definition in the override hierarchy is rewritten too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from .. import constants as cs
from .contract import Reingest, change_signature_expectation, measure, verify
from .signature_planner import SignaturePlanner
from .signature_types import (
    ParamSpec,
    RewrittenSite,
    SignatureRefused,
    SignatureReport,
    UnmappedSite,
    parse_param_spec,
    sites_for,
)
from .transaction import EditTransaction, StagedTree, VerificationResult, undo_last


class SignatureChanger(SignaturePlanner):
    def apply(
        self, qn: str, specs: Iterable[ParamSpec], allow_heuristic: bool = False
    ) -> SignatureReport:
        # The contract must know the caller waived the guess check, or it
        # rolls back every rewrite of a heuristic site it was asked for.
        self._allow_heuristic = allow_heuristic
        report, patcher = self.plan(qn, specs, allow_heuristic)
        tx = EditTransaction(self.repo_root)
        results = patcher.stage_into(tx)
        broken = [key for key, result in results.items() if result.parses is False]
        if broken:
            tx.rollback()
            return report._replace(
                files=tuple(sorted(results)),
                message=cs.SIGNATURE_PARSE_FAILED.format(files=", ".join(broken)),
            )

        def verifier(tree: StagedTree) -> VerificationResult | bool | None:
            return self.verify(tree) if self.verify is not None else True

        outcome = tx.commit(verifier)
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            report = self._enforce_contract(report)
        return report

    def _enforce_contract(self, report: SignatureReport) -> SignatureReport:
        assert self.reingest is not None
        delta = measure(
            self.fetch_all, self.project, self.repo_root, report.files, self.reingest
        )
        verdict = verify(
            change_signature_expectation(
                (f"{u.path}:{u.line}" for u in report.unmapped),
                heuristic_allowed=self._allow_heuristic,
            ),
            delta,
            rewritten=[
                (f"{s.path}:{s.line}", s.resolution)
                for s in report.sites
                if s.before != s.after
            ],
        )
        if verdict.ok:
            return report._replace(verdict=verdict)
        undo_last(self.repo_root)
        self.reingest(list(report.files))
        return report._replace(
            applied=False,
            verdict=verdict,
            message=cs.SIGNATURE_CONTRACT_FAILED.format(
                reasons="; ".join(verdict.failures)
            ),
        )


def change_signature(
    repo_root: Path,
    fetch_all: Callable[[str, dict[str, object]], list[dict[str, object]]],
    project_name: str,
    qualified_name: str,
    new_params: Iterable[ParamSpec | str],
    allow_heuristic: bool = False,
    dry_run: bool = False,
    verify: Callable[[StagedTree], VerificationResult | bool | None] | None = None,
    reingest: Reingest | None = None,
) -> SignatureReport:
    """The op: plan (rewrites definitions and mapped sites) or plan and apply."""
    specs = [parse_param_spec(p) if isinstance(p, str) else p for p in new_params]
    changer = SignatureChanger(
        repo_root, fetch_all, project_name, verify=verify, reingest=reingest
    )
    if dry_run:
        report, _patcher = changer.plan(qualified_name, specs, allow_heuristic)
        return report
    return changer.apply(qualified_name, specs, allow_heuristic)


__all__ = [
    "ParamSpec",
    "RewrittenSite",
    "SignatureChanger",
    "SignatureRefused",
    "SignatureReport",
    "UnmappedSite",
    "change_signature",
    "parse_param_spec",
    "sites_for",
]
