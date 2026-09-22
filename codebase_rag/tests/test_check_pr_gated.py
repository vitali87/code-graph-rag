"""The gate must not claim more than it checked (#1941).

`check_pr_gated.py` answers "are the REQUIRED contexts present and satisfied at
this head". A check the branch ruleset does not require is outside that
question, so a red one is correctly not a blocking reason -- and it was also
absent from the output entirely, under a verdict line reading `gated (every
check present and satisfied)`. A reader told that this script's output is the
state merges on that sentence.

Measured on PR #1930: 32 contexts, `Analyze (csharp)` FAILURE, reported gated
with no reason and no caveat.
"""

from __future__ import annotations

import json

import pytest

from scripts import check_pr_gated
from scripts.check_pr_gated import (
    REQUIRED_CONTEXT,
    UNREQUIRED_CAVEAT_LIMIT,
    failing_unrequired_contexts,
    unrequired_failure_caveat,
)


def check_run(name: str, conclusion: str) -> dict[str, object]:
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion}


def status_context(context: str, state: str) -> dict[str, object]:
    return {"__typename": "StatusContext", "context": context, "state": state}


def test_a_failing_unrequired_check_is_reported() -> None:
    """The reported shape, in its reported form: the required aggregate is
    green and one unrequired analysis job is red."""
    rollup = [
        check_run(REQUIRED_CONTEXT, "SUCCESS"),
        check_run("Analyze (csharp)", "FAILURE"),
    ]

    assert failing_unrequired_contexts(rollup) == ["Analyze (csharp)"]
    caveat = unrequired_failure_caveat(rollup)
    assert len(caveat) == 1
    assert "Analyze (csharp)" in caveat[0]
    assert "do not block" in caveat[0]
    # The caveat may not upgrade "this gate does not require it" into "the
    # ruleset does not require it": the gate never reads that list.
    assert "this gate does not require" in caveat[0]
    assert "does not read the ruleset's required-context list" in caveat[0]
    assert "the ruleset does not require" not in caveat[0]


def test_a_green_board_produces_no_caveat() -> None:
    """The accept control. A caveat on every PR is one nobody reads, which is
    the failure this is meant to prevent rather than cause."""
    rollup = [
        check_run(REQUIRED_CONTEXT, "SUCCESS"),
        check_run("Analyze (csharp)", "SUCCESS"),
        status_context("sonarcloud", "SUCCESS"),
    ]

    assert failing_unrequired_contexts(rollup) == []
    assert unrequired_failure_caveat(rollup) == []


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL"])
def test_a_conditional_job_that_did_not_apply_is_not_a_failure(conclusion: str) -> None:
    """Branch protection treats both as satisfied. Counting them would fire the
    caveat on almost every PR with a conditional job."""
    rollup = [check_run(REQUIRED_CONTEXT, "SUCCESS"), check_run("Docs", conclusion)]

    assert failing_unrequired_contexts(rollup) == []


@pytest.mark.parametrize(
    "entry",
    [
        # A queued CheckRun reports `conclusion: ""`, an empty STRING rather
        # than null, which is the shape `is_concluded` exists for.
        check_run("Slow Job", ""),
        # A StatusContext has no `conclusion` at all; unfinished is carried in
        # `state`, and these are the two values that mean it.
        status_context("third-party", "PENDING"),
        status_context("third-party", "EXPECTED"),
    ],
    ids=["queued check run", "pending status", "expected status"],
)
def test_a_check_that_has_not_finished_is_not_a_failure(
    entry: dict[str, object],
) -> None:
    """Reading an unfinished check as failed would report a caveat about work
    still in flight, on every PR whose board is still filling in."""
    rollup = [check_run(REQUIRED_CONTEXT, "SUCCESS"), entry]

    assert failing_unrequired_contexts(rollup) == []


def test_a_third_party_status_is_read_through_state() -> None:
    """A `StatusContext` carries `context` and `state` and has no `name` or
    `conclusion` key at all, so a predicate written only for a `CheckRun`
    silently never sees one."""
    rollup = [
        check_run(REQUIRED_CONTEXT, "SUCCESS"),
        status_context("licence/cla", "FAILURE"),
        status_context("deploy/preview", "ERROR"),
    ]

    assert failing_unrequired_contexts(rollup) == ["deploy/preview", "licence/cla"]


def test_the_required_context_is_not_repeated_as_a_caveat() -> None:
    """A red `All Checks Pass` is already a blocking reason. Naming it here as
    well would read as two separate problems."""
    rollup = [check_run(REQUIRED_CONTEXT, "FAILURE")]

    assert failing_unrequired_contexts(rollup) == []


def test_a_job_the_aggregate_covers_is_still_named() -> None:
    """The other direction, and deliberate. `All Checks Pass` reports one
    verdict over every job it aggregates, so when it is red the caveat is what
    names the job that actually failed."""
    rollup = [
        check_run(REQUIRED_CONTEXT, "FAILURE"),
        check_run("Type Check", "FAILURE"),
    ]

    assert failing_unrequired_contexts(rollup) == ["Type Check"]


def test_many_failures_are_summarised_rather_than_listed() -> None:
    """An Actions outage reds many contexts at once, which is the window this
    was filed during. A caveat listing thirty names is one nobody reads."""
    rollup = [check_run(REQUIRED_CONTEXT, "SUCCESS")] + [
        check_run(f"Analyze ({index})", "FAILURE") for index in range(20)
    ]

    caveat = unrequired_failure_caveat(rollup)[0]
    assert caveat.startswith("20 check(s)")
    assert f"(+{20 - UNREQUIRED_CAVEAT_LIMIT} more)" in caveat
    # Count the names actually listed, not the commas in the sentence: the
    # closing clause carries one of its own.
    listed = caveat.split("head: ", 1)[1].split(" (+", 1)[0]
    assert len(listed.split(", ")) == UNREQUIRED_CAVEAT_LIMIT


def test_a_name_is_reported_once_however_many_entries_carry_it() -> None:
    """A rerun leaves two entries under one name. Two lines for one failure
    inflates the count the caveat opens with."""
    rollup = [
        check_run(REQUIRED_CONTEXT, "SUCCESS"),
        check_run("Analyze (csharp)", "FAILURE"),
        check_run("Analyze (csharp)", "CANCELLED"),
    ]

    assert failing_unrequired_contexts(rollup) == ["Analyze (csharp)"]
    assert unrequired_failure_caveat(rollup)[0].startswith("1 check(s)")


def _gate_a_green_pr(
    monkeypatch: pytest.MonkeyPatch,
    rollup: list[dict[str, object]],
    *,
    rules: list[dict[str, object]] | None = None,
    protection: dict[str, object] | None = None,
    reviews: list[dict[str, object]] | None = None,
    repo: dict[str, object] | None = None,
    protection_error: str | None = None,
) -> tuple[list[str], list[str]]:
    """Run `check()` over a PR that is gated on every other axis.

    The helpers above grade the predicate; this grades the WIRING. A caveat
    the predicate produces and `check()` never appends is invisible to the
    reader, which is the whole of #1941, and a test that only calls the
    predicate cannot see the difference.
    """
    head = "a" * 40
    view = {
        "headRefOid": head,
        "baseRefName": "main",
        "statusCheckRollup": rollup,
        "comments": [
            {
                "body": "Actionable comments posted: 0",
                "author": {"login": "greptile-apps[bot]"},
            }
        ],
        "reviews": reviews or [],
    }
    ruleset = rules if rules is not None else [{"type": "required_status_checks"}]

    def fake_gh(*args: str) -> str:
        if args[:2] == ("pr", "view"):
            return json.dumps(view)
        if args[0] == "api" and args[1].startswith("repos/") and "/rules/" in args[1]:
            return json.dumps(ruleset)
        if args[0] == "api" and args[1].endswith("/protection"):
            # A repo with no classic layer 404s (see fake_result below).
            return json.dumps(protection) if protection is not None else ""
        if args[0] == "api" and args[1] == f"repos/{check_pr_gated.REPO}":
            return json.dumps(repo if repo is not None else {"allow_auto_merge": False})
        if args[0] == "api" and "/actions/runs/" in args[1]:
            return json.dumps({"pull_requests": [{"number": 1930}]})
        return ""

    def fake_result(*args: str) -> tuple[str, str, int]:
        # The classic endpoint through `_gh_result`: absent reads as a 404,
        # `protection_error` stands in for a 401 or a network failure.
        if protection_error is not None:
            return "", protection_error, 1
        if protection is None:
            return "", "gh: Branch not protected (HTTP 404)", 1
        return json.dumps(protection), "", 0

    monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake_gh)
    monkeypatch.setattr(check_pr_gated, "_gh_result", fake_result)
    monkeypatch.setattr(
        check_pr_gated, "ci_runs_at_head", lambda _head: [{"id": 1, "path": "x"}]
    )
    monkeypatch.setattr(check_pr_gated, "_unresolved_thread_count", lambda _pr: (0, ""))
    monkeypatch.setattr(check_pr_gated, "missing_aggregated_jobs", lambda _rollup: [])
    return check_pr_gated.check("1930")


def test_check_surfaces_the_caveat_on_an_otherwise_gated_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported measurement, end to end: the gate is satisfied, so there is
    no reason, and the red unrequired check must still reach the reader."""
    reasons, caveats = _gate_a_green_pr(
        monkeypatch,
        [
            check_run(REQUIRED_CONTEXT, "SUCCESS"),
            check_run("Analyze (csharp)", "FAILURE"),
        ],
    )

    assert reasons == []
    assert any("Analyze (csharp)" in caveat for caveat in caveats)


def test_a_fully_green_pr_still_gates_with_no_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the wiring: the caveat channel stays quiet when there is
    nothing to say, so a reader who sees one knows it means something."""
    reasons, caveats = _gate_a_green_pr(
        monkeypatch, [check_run(REQUIRED_CONTEXT, "SUCCESS")]
    )

    assert (reasons, caveats) == ([], [])


def test_the_verdict_line_claims_only_what_was_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`every check present and satisfied` is a claim about every check, and
    the script only ever looked at one. `every REQUIRED check` was no better:
    the gate reads the branch rule's type, never its required-context list, so
    it cannot speak for what the ruleset requires. A reader told that this
    output is the state merges on that sentence."""
    monkeypatch.setattr(check_pr_gated, "check", lambda _pr: ([], []))

    assert check_pr_gated.main(["check_pr_gated.py", "1930"]) == 0

    line = capsys.readouterr().out
    assert f"'{check_pr_gated.REQUIRED_CONTEXT}' present and satisfied" in line
    assert "no other context was tested for being required" in line
    assert "every check present and satisfied" not in line
    assert "every REQUIRED check" not in line


def test_a_caveat_prints_above_the_gated_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Order matters here and is already the file's stated rule: a caveat under
    a `gated` line is the one a reader skips."""
    monkeypatch.setattr(
        check_pr_gated, "check", lambda _pr: ([], ["1 check(s) ... Analyze (csharp)"])
    )

    assert check_pr_gated.main(["check_pr_gated.py", "1930"]) == 0

    out = capsys.readouterr().out
    assert out.index("Analyze (csharp)") < out.index("gated (")
    assert "see caveat(s) above" in out


# --- the classic branch-protection layer (issue #1957) -----------------------

_GREEN = [check_run(REQUIRED_CONTEXT, "SUCCESS")]
_CLASSIC_ONE_APPROVAL: dict[str, object] = {
    "required_pull_request_reviews": {"required_approving_review_count": 1},
    "enforce_admins": {"enabled": False},
    "required_status_checks": None,
}
_RULESET_ZERO: list[dict[str, object]] = [
    {"type": "required_status_checks"},
    {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
]


def _review(login: str, state: str) -> dict[str, object]:
    return {"author": {"login": login}, "state": state}


def test_no_classic_layer_adds_no_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    reasons, caveats = _gate_a_green_pr(monkeypatch, _GREEN, protection=None)
    assert reasons == []
    assert caveats == []


def test_a_classic_approval_requirement_the_pr_does_not_meet_is_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured case: ruleset 0, classic 1, zero approvals, every check
    green -- `gh pr merge` refuses with 'base branch policy prohibits'."""
    reasons, caveats = _gate_a_green_pr(
        monkeypatch, _GREEN, rules=_RULESET_ZERO, protection=_CLASSIC_ONE_APPROVAL
    )
    assert len(reasons) == 1
    reason = reasons[0]
    assert "requires 1 approving review(s) (classic branch protection)" in reason
    assert "has 0" in reason
    assert "base branch policy prohibits the merge" in reason
    # The remedies are read from the repo, not asserted from a general message.
    assert "auto-merge is disabled" in reason
    assert "`--admin` would bypass" in reason
    # The disagreement between the layers is itself reported.
    assert len(caveats) == 1
    assert "disagree on approvals (0 vs 1)" in caveats[0]


def test_an_approving_review_satisfies_the_classic_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasons, _caveats = _gate_a_green_pr(
        monkeypatch,
        _GREEN,
        rules=_RULESET_ZERO,
        protection=_CLASSIC_ONE_APPROVAL,
        reviews=[_review("reviewer", "APPROVED")],
    )
    assert reasons == []


def test_layers_that_agree_produce_no_disagreement_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules: list[dict[str, object]] = [
        {"type": "required_status_checks"},
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
    ]
    _reasons, caveats = _gate_a_green_pr(
        monkeypatch,
        _GREEN,
        rules=rules,
        protection=_CLASSIC_ONE_APPROVAL,
        reviews=[_review("reviewer", "APPROVED")],
    )
    assert caveats == []


def test_enforced_for_admins_names_no_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    protection: dict[str, object] = {
        **_CLASSIC_ONE_APPROVAL,
        "enforce_admins": {"enabled": True},
    }
    reasons, _ = _gate_a_green_pr(monkeypatch, _GREEN, protection=protection)
    assert "enforced for administrators too" in reasons[0]
    assert "--admin" not in reasons[0]


def test_auto_merge_enabled_is_named_as_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasons, _ = _gate_a_green_pr(
        monkeypatch,
        _GREEN,
        protection=_CLASSIC_ONE_APPROVAL,
        repo={"allow_auto_merge": True},
    )
    assert "auto-merge is enabled" in reasons[0]


def test_classic_status_checks_count_as_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ruleset requires checks, but the classic layer does: something IS
    enforced, so the 'nothing is enforced' reason must not fire."""
    protection: dict[str, object] = {
        "required_status_checks": {"contexts": [REQUIRED_CONTEXT]},
        "enforce_admins": {"enabled": False},
    }
    reasons, _ = _gate_a_green_pr(monkeypatch, _GREEN, rules=[], protection=protection)
    assert reasons == []


def test_a_classic_required_context_absent_at_the_head_is_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protection: dict[str, object] = {
        "required_status_checks": {"contexts": [REQUIRED_CONTEXT, "Extra Gate"]},
        "enforce_admins": {"enabled": False},
    }
    reasons, _ = _gate_a_green_pr(monkeypatch, _GREEN, protection=protection)
    assert reasons == [
        "context 'Extra Gate', required by classic branch protection, is absent "
        "at the head"
    ]
    reasons, _ = _gate_a_green_pr(
        monkeypatch,
        [*_GREEN, check_run("Extra Gate", "FAILURE")],
        protection=protection,
    )
    assert reasons == [
        "'Extra Gate' (required by classic branch protection) concluded FAILURE"
    ]


def test_approvals_count_each_reviewers_latest_verdict() -> None:
    # Counting FIRST verdicts would give 2 (a, b); counting any approval
    # ever, 3. Only the latest verdict per reviewer gives 1.
    reviews = [
        _review("a", "APPROVED"),
        _review("a", "COMMENTED"),  # a comment changes nothing
        _review("b", "APPROVED"),
        _review("b", "CHANGES_REQUESTED"),  # withdrawn
        _review("c", "CHANGES_REQUESTED"),
        _review("coderabbitai", "APPROVED"),  # the shape gh returns; never counts
    ]
    assert check_pr_gated.approvals(reviews) == 1
    # Granted after changes were requested: the latest verdict wins.
    assert check_pr_gated.approvals([*reviews, _review("c", "APPROVED")]) == 2


def test_ruleset_and_classic_counts_are_read_from_their_own_shapes() -> None:
    assert check_pr_gated.ruleset_review_count(_RULESET_ZERO) == 0
    assert check_pr_gated.ruleset_review_count([{"type": "deletion"}]) is None
    assert check_pr_gated.classic_review_count(_CLASSIC_ONE_APPROVAL) == 1
    assert check_pr_gated.classic_review_count({}) is None
    assert check_pr_gated.classic_required_contexts(_CLASSIC_ONE_APPROVAL) == []


def test_an_unreadable_protection_endpoint_is_a_reason_not_an_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 or a network failure must not read as "no classic layer": that
    is the fail-open the script exists to close (local review P1)."""
    reasons, _ = _gate_a_green_pr(
        monkeypatch, _GREEN, protection_error="gh: Bad credentials (HTTP 401)"
    )
    assert reasons == [
        "could not read classic branch protection on 'main' (gh: Bad credentials "
        "(HTTP 401)), so its approval and status-check requirements are unverified"
    ]


def test_a_classic_required_failure_is_a_reason_not_also_a_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One red check, one report: the classic-required context is a reason,
    so the unrequired-failure caveat leaves it out (local review P2)."""
    protection: dict[str, object] = {
        "required_status_checks": {"contexts": [REQUIRED_CONTEXT, "Extra Gate"]},
        "enforce_admins": {"enabled": False},
    }
    reasons, caveats = _gate_a_green_pr(
        monkeypatch,
        [*_GREEN, check_run("Extra Gate", "FAILURE"), check_run("Other", "FAILURE")],
        protection=protection,
    )
    assert reasons == [
        "'Extra Gate' (required by classic branch protection) concluded FAILURE"
    ]
    assert len(caveats) == 1
    assert "Other" in caveats[0]
    assert "Extra Gate" not in caveats[0]


def _status(context: str, state: str, **extra: object) -> dict[str, object]:
    return {"__typename": "StatusContext", "context": context, "state": state, **extra}


def test_a_classic_required_status_context_is_judged_by_its_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit status carries `state`, not `conclusion`; SKIPPED and
    NEUTRAL check runs satisfy a required check as GitHub does (bot review
    on PR #1968)."""
    protection: dict[str, object] = {
        "required_status_checks": {
            "contexts": [REQUIRED_CONTEXT, "Third Party", "Optional Job"]
        },
        "enforce_admins": {"enabled": False},
    }
    reasons, _ = _gate_a_green_pr(
        monkeypatch,
        [
            *_GREEN,
            _status("Third Party", "SUCCESS"),
            check_run("Optional Job", "SKIPPED"),
        ],
        protection=protection,
    )
    assert reasons == []
    reasons, _ = _gate_a_green_pr(
        monkeypatch,
        [
            *_GREEN,
            _status("Third Party", "FAILURE"),
            check_run("Optional Job", "NEUTRAL"),
        ],
        protection=protection,
    )
    assert reasons == [
        "'Third Party' (required by classic branch protection) concluded FAILURE"
    ]


def test_a_rerun_context_is_judged_by_its_latest_entry_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protection: dict[str, object] = {
        "required_status_checks": {"contexts": [REQUIRED_CONTEXT, "Extra Gate"]},
        "enforce_admins": {"enabled": False},
    }
    stale = {
        **check_run("Extra Gate", "FAILURE"),
        "startedAt": "2026-09-19T00:00:00Z",
    }
    cancelled = {
        **check_run("Extra Gate", "CANCELLED"),
        "startedAt": "2026-09-19T00:01:00Z",
    }
    fresh = {
        **check_run("Extra Gate", "SUCCESS"),
        "startedAt": "2026-09-19T00:02:00Z",
    }
    reasons, _ = _gate_a_green_pr(
        monkeypatch, [*_GREEN, fresh, stale, cancelled], protection=protection
    )
    assert reasons == []
    # The other way round, one reason, not one per stale entry.
    late_failure = {
        **check_run("Extra Gate", "FAILURE"),
        "startedAt": "2026-09-19T00:03:00Z",
    }
    reasons, _ = _gate_a_green_pr(
        monkeypatch, [*_GREEN, fresh, late_failure, stale], protection=protection
    )
    assert reasons == [
        "'Extra Gate' (required by classic branch protection) concluded FAILURE"
    ]


def test_the_admin_remedy_is_claimed_only_for_a_classic_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`enforce_admins` is a classic setting; a ruleset's bypass list is not
    read here, so the message must not promise `--admin` for it."""
    rules: list[dict[str, object]] = [
        {"type": "required_status_checks"},
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
    ]
    reasons, _ = _gate_a_green_pr(monkeypatch, _GREEN, rules=rules, protection=None)
    assert "(ruleset)" in reasons[0]
    assert "bypass list" in reasons[0]
    assert "would bypass it" not in reasons[0]


def test_an_in_progress_rerun_outranks_a_run_that_completed_after_it_began(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recency is when a run STARTED, one event for every entry: an old
    failure that completed after the rerun began is not the latest run
    (bot review on PR #1968, second round)."""
    protection: dict[str, object] = {
        "required_status_checks": {"contexts": [REQUIRED_CONTEXT, "Extra Gate"]},
        "enforce_admins": {"enabled": False},
    }
    old_failure = {
        **check_run("Extra Gate", "FAILURE"),
        "startedAt": "2026-09-19T12:00:00Z",
        "completedAt": "2026-09-19T12:30:00Z",
    }
    rerun = {
        "__typename": "CheckRun",
        "name": "Extra Gate",
        "status": "IN_PROGRESS",
        "conclusion": "",
        "startedAt": "2026-09-19T12:15:00Z",
    }
    reasons, _ = _gate_a_green_pr(
        monkeypatch, [*_GREEN, old_failure, rerun], protection=protection
    )
    assert reasons == [
        "'Extra Gate' (required by classic branch protection) has not concluded"
    ]


def test_a_status_and_a_check_run_sharing_a_name_must_both_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub requires both when a required check and a commit status share
    a name; collapsing them let a later green check run hide a failing
    status (bot review on PR #1968, second round)."""
    protection: dict[str, object] = {
        "required_status_checks": {"contexts": [REQUIRED_CONTEXT, "Extra Gate"]},
        "enforce_admins": {"enabled": False},
    }
    failing_status = _status("Extra Gate", "FAILURE", createdAt="2026-09-19T00:00:00Z")
    green_run = {
        **check_run("Extra Gate", "SUCCESS"),
        "startedAt": "2026-09-19T00:05:00Z",
    }
    reasons, _ = _gate_a_green_pr(
        monkeypatch, [*_GREEN, failing_status, green_run], protection=protection
    )
    assert reasons == [
        "'Extra Gate' (required by classic branch protection) concluded FAILURE"
    ]


# --- A required check bound to one GitHub App (#2131) ---

_BOUND_APP = 15368
_OTHER_APP = 999


def _app_run(app: int, conclusion: str = "success") -> dict[str, object]:
    return {
        "name": REQUIRED_CONTEXT,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-09-22T10:00:00Z",
        "app": {"id": app},
    }


_CLASSIC_BOUND: dict[str, object] = {
    "required_status_checks": {
        "contexts": [REQUIRED_CONTEXT],
        "checks": [{"context": REQUIRED_CONTEXT, "app_id": _BOUND_APP}],
    },
    "enforce_admins": {"enabled": False},
}
_RULESET_BOUND: list[dict[str, object]] = [
    {
        "type": "required_status_checks",
        "parameters": {
            "required_status_checks": [
                {"context": REQUIRED_CONTEXT, "integration_id": _BOUND_APP}
            ]
        },
    }
]


def test_bindings_are_read_from_both_layers_and_any_source_is_unbound() -> None:
    protection = {
        "required_status_checks": {
            "checks": [
                {"context": "A", "app_id": 1},
                {"context": "B", "app_id": -1},
                {"context": "C", "app_id": None},
                {"context": "D"},
            ]
        }
    }
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "A", "integration_id": 2},
                    {"context": "E", "integration_id": 3},
                    {"context": "F"},
                ]
            },
        }
    ]
    assert check_pr_gated.required_app_bindings(rules, protection) == {
        "A": {1, 2},
        "E": {3},
    }


@pytest.mark.parametrize(
    ("rules", "protection"),
    [
        pytest.param(None, _CLASSIC_BOUND, id="classic-app_id"),
        pytest.param(_RULESET_BOUND, None, id="ruleset-integration_id"),
    ],
)
def test_a_same_name_check_from_another_app_does_not_satisfy_a_binding(
    monkeypatch: pytest.MonkeyPatch,
    rules: list[dict[str, object]] | None,
    protection: dict[str, object] | None,
) -> None:
    """The name-only rollup is green, but the only run of the name comes
    from another App, which GitHub does not accept (#2131)."""
    monkeypatch.setattr(
        check_pr_gated, "head_check_runs", lambda _h, _n: [_app_run(_OTHER_APP)]
    )
    reasons, _ = _gate_a_green_pr(
        monkeypatch, _GREEN, rules=rules, protection=protection
    )
    assert reasons == [
        f"'{REQUIRED_CONTEXT}' is required from App {_BOUND_APP}, but no check "
        f"run of that name at the head comes from it (posted by App(s) {_OTHER_APP})"
    ]


@pytest.mark.parametrize(
    ("rules", "protection"),
    [
        pytest.param(None, _CLASSIC_BOUND, id="classic-app_id"),
        pytest.param(_RULESET_BOUND, None, id="ruleset-integration_id"),
    ],
)
def test_the_bound_apps_green_run_satisfies_it(
    monkeypatch: pytest.MonkeyPatch,
    rules: list[dict[str, object]] | None,
    protection: dict[str, object] | None,
) -> None:
    """The accept control: the bound App's own green run is enough, beside a
    red run of the same name from another App."""
    monkeypatch.setattr(
        check_pr_gated,
        "head_check_runs",
        lambda _h, _n: [_app_run(_OTHER_APP, "failure"), _app_run(_BOUND_APP)],
    )
    reasons, _ = _gate_a_green_pr(
        monkeypatch, _GREEN, rules=rules, protection=protection
    )
    assert reasons == []


def test_the_bound_apps_failed_run_is_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_pr_gated,
        "head_check_runs",
        lambda _h, _n: [_app_run(_BOUND_APP, "failure"), _app_run(_OTHER_APP)],
    )
    reasons, _ = _gate_a_green_pr(monkeypatch, _GREEN, protection=_CLASSIC_BOUND)
    assert reasons == [f"'{REQUIRED_CONTEXT}' from App {_BOUND_APP} concluded FAILURE"]


def test_unreadable_check_runs_leave_a_binding_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: an unread provider is not a satisfied one."""
    monkeypatch.setattr(check_pr_gated, "head_check_runs", lambda _h, _n: None)
    reasons, _ = _gate_a_green_pr(monkeypatch, _GREEN, rules=_RULESET_BOUND)
    assert len(reasons) == 1
    assert "could not be read" in reasons[0]
    assert "unverified" in reasons[0]


def test_an_unbound_requirement_makes_no_check_runs_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No binding, no extra API call: this repo's own requirement is unbound."""

    def unexpected(_head: str, _name: str) -> list[dict[str, object]]:
        raise AssertionError("check runs read for an unbound requirement")

    monkeypatch.setattr(check_pr_gated, "head_check_runs", unexpected)
    reasons, _ = _gate_a_green_pr(monkeypatch, _GREEN)
    assert reasons == []
