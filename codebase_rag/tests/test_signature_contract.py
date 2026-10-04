# Unit tests for the postcondition contract of an applied `change_signature`
# (issues #1531, #1533): `enforce_contract` measures the change through a
# stubbed structural delta and keeps it, undoes it, or reports why it could
# do neither. The transaction log and the re-ingest are stubbed; `verify`
# runs for real.

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import signature_contract
from codebase_rag.editing.signature_contract import enforce_contract
from codebase_rag.editing.signature_spec import (
    SignatureReport,
    SignatureSite,
    UnmappedSite,
)
from codebase_rag.editing.transaction import TransactionConflict
from codebase_rag.graph_updater import ReingestAborted
from codebase_rag.structural_delta import StructuralDelta
from codebase_rag.types_defs import ReingestReport

ROOT = Path("/repo")
TXN = "txn-1"


def _delta(arity_findings: list[dict[str, object]] | None = None) -> StructuralDelta:
    delta: dict[str, object] = {
        "paths": ["pkg/util.py"],
        "reparsed": ["pkg/util.py"],
        "affected": [],
        "removed_files": [],
        "symbols": {"added": [], "removed": [], "renamed": [], "changed": []},
        "dangling_callers": [],
        "signature_changes": [],
        "arity_findings": arity_findings or [],
        "new_duplicates": [],
        "new_import_cycles": [],
        "stale_importers": [],
        "tests_reaching": [],
        "call_sites": {"before": 2, "after": 2},
        "reingest_ms": 1.0,
        "delta_ms": 1.0,
    }
    return cast(StructuralDelta, delta)


def _finding(path: str, line: int, col: int) -> dict[str, object]:
    return {
        "caller": "p.pkg.app.run",
        "path": path,
        "line": line,
        "col": col,
        "target": "p.pkg.util.helper",
        "verdict": cs.DELTA_ARITY_TOO_MANY,
    }


def _report(
    resolution: str = "exact",
    unmapped: tuple[UnmappedSite, ...] = (),
) -> SignatureReport:
    return SignatureReport(
        qualified_name="p.pkg.util.helper",
        old_params=("a",),
        new_params=("a", "b=1"),
        applied=True,
        transaction_id=TXN,
        files=("pkg/util.py", "pkg/app.py"),
        sites=(
            SignatureSite("definition", "pkg/util.py", 1, 0, "p.pkg.util.helper", None),
            SignatureSite("call", "pkg/app.py", 5, 11, "p.pkg.app.run", resolution),
        ),
        unmapped=unmapped,
        hierarchy=(),
        diff="",
        message="applied",
    )


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.delta: StructuralDelta | Exception = _delta()
        self.undo_error: Exception | None = None
        self.reingest_error: Exception | None = None
        self.undone: list[tuple[Path, str]] = []
        self.reingested: list[list[str]] = []
        self.measured: list[tuple[str, ...]] = []
        monkeypatch.setattr(signature_contract, "measure", self._measure)
        monkeypatch.setattr(signature_contract, "undo_transaction", self._undo)

    def _measure(self, fetch_all, project, repo_root, files, reingest):  # noqa: ANN001, ANN202
        assert (project, repo_root) == ("p", ROOT)
        self.measured.append(tuple(files))
        if isinstance(self.delta, Exception):
            raise self.delta
        return self.delta

    def _undo(self, repo_root: Path, transaction_id: str) -> None:
        if self.undo_error is not None:
            raise self.undo_error
        self.undone.append((repo_root, transaction_id))

    def reingest(self, paths: list[str]) -> ReingestReport:
        if self.reingest_error is not None:
            raise self.reingest_error
        self.reingested.append(paths)
        return ReingestReport(tuple(paths), (), (), (), 1.0)

    def run(self, report: SignatureReport, allow_heuristic: bool = False):  # noqa: ANN201
        return enforce_contract(
            report,
            allow_heuristic,
            fetch_all=lambda *_a, **_k: [],
            project="p",
            repo_root=ROOT,
            reingest=self.reingest,
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


def test_passing_change_is_kept_with_its_verdict(harness: _Harness) -> None:
    result = harness.run(_report())
    assert harness.measured == [("pkg/util.py", "pkg/app.py")]
    assert result.applied
    assert result.verdict is not None
    assert result.verdict.ok
    assert result.message == "applied"
    assert harness.undone == []


def test_listed_unmapped_site_is_keyed_by_column(harness: _Harness) -> None:
    # Two calls on line 7: the one at col 4 is listed, the one at col 20 is not.
    harness.delta = _delta(
        [_finding("pkg/app.py", 7, 4), _finding("pkg/app.py", 7, 20)]
    )
    listed = (
        UnmappedSite("p.pkg.app.run", "pkg/app.py", 7, 4, "why"),
        # A site with no location cannot be keyed and never excuses one.
        UnmappedSite("p.pkg.app.run", "pkg/app.py", None, None, "no location"),
    )
    result = harness.run(_report(unmapped=listed))
    assert result.verdict is not None
    assert not result.verdict.ok
    assert result.verdict.failures == (
        cs.CONTRACT_SITES_UNMAPPED.format(sites="pkg/app.py:7:20 (too_many)"),
    )
    assert not result.applied
    assert harness.undone == [(ROOT, TXN)]

    harness.undone.clear()
    harness.delta = _delta([_finding("pkg/app.py", 7, 4)])
    kept = harness.run(_report(unmapped=listed))
    assert kept.applied
    assert kept.verdict is not None
    assert kept.verdict.ok
    assert harness.undone == []


def test_failed_postcondition_undoes_this_transaction(harness: _Harness) -> None:
    result = harness.run(_report(resolution="heuristic"))
    reasons = cs.CONTRACT_HEURISTIC_REWRITTEN.format(sites="pkg/app.py:5:11")
    assert harness.undone == [(ROOT, TXN)]
    assert harness.reingested == [["pkg/util.py", "pkg/app.py"]]
    assert not result.applied
    assert not result.graph_incomplete
    assert result.message == cs.SIGNATURE_CONTRACT_FAILED.format(reasons=reasons)


def test_heuristic_rewrite_is_kept_when_allowed(harness: _Harness) -> None:
    result = harness.run(_report(resolution="heuristic"), allow_heuristic=True)
    assert result.applied
    assert result.verdict is not None
    assert result.verdict.ok
    assert harness.undone == []


def test_rollback_refused_by_a_later_edit_keeps_the_change(
    harness: _Harness,
) -> None:
    harness.undo_error = TransactionConflict("stacked")
    result = harness.run(_report(resolution="heuristic"))
    reasons = cs.CONTRACT_HEURISTIC_REWRITTEN.format(sites="pkg/app.py:5:11")
    assert result.applied
    assert result.verdict is not None
    assert not result.verdict.ok
    assert result.message == cs.SIGNATURE_ROLLBACK_REFUSED.format(reasons=reasons)
    assert harness.reingested == []


def test_rollback_whose_reingest_fails_flags_the_graph(harness: _Harness) -> None:
    harness.reingest_error = RuntimeError("db gone")
    result = harness.run(_report(resolution="heuristic"))
    reasons = cs.CONTRACT_HEURISTIC_REWRITTEN.format(sites="pkg/app.py:5:11")
    assert harness.undone == [(ROOT, TXN)]
    assert not result.applied
    assert result.graph_incomplete
    assert result.message == cs.SIGNATURE_ROLLBACK_UNMEASURED.format(
        reasons=reasons, error="db gone"
    )


@pytest.mark.parametrize(
    ("error", "incomplete"),
    [
        (ValueError("bad path"), False),
        (ReingestAborted("read failed"), False),
        (RuntimeError("mid-write"), True),
    ],
)
def test_unmeasurable_change_is_reported_not_raised(
    harness: _Harness, error: Exception, incomplete: bool
) -> None:
    harness.delta = error
    result = harness.run(_report())
    assert result.applied
    assert result.verdict is None
    assert result.graph_incomplete is incomplete
    assert result.message == cs.SIGNATURE_CONTRACT_UNMEASURED.format(error=error)
    assert harness.undone == []
