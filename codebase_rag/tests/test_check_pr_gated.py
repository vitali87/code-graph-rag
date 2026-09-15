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
    monkeypatch: pytest.MonkeyPatch, rollup: list[dict[str, object]]
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
        "reviews": [],
    }

    def fake_gh(*args: str) -> str:
        if args[:2] == ("pr", "view"):
            return json.dumps(view)
        if args[0] == "api" and args[1].startswith("repos/") and "/rules/" in args[1]:
            return json.dumps([{"type": "required_status_checks"}])
        if args[0] == "api" and "/actions/runs/" in args[1]:
            return json.dumps({"pull_requests": [{"number": 1930}]})
        return ""

    monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake_gh)
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
