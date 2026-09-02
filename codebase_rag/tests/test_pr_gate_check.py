"""The PR-readiness checker's own traps, each taken from real GitHub JSON.

Every fixture below was captured from a live PR in this repository rather
than written by hand, because the whole point of the checker is that the
naive reading of these payloads is wrong in a way that reads as success
(issues #1581, #1582).

The four that bite, and which a naive implementation gets wrong:

* `statusCheckRollup` mixes two shapes. A `CheckRun` carries `name`; a
  `StatusContext` carries `context` and has no `name` key at all, so
  `select(.name | test(...))` raises mid-pipeline and the error reads as
  "no matching contexts" if stderr scrolls past.
* A queued check reports `conclusion: ""` -- an empty STRING, not null --
  so a `.conclusion // "pending"` default never fires and the check looks
  concluded.
* `Integration Tests` and `Binary Smoke Test` match a loose `/test/i` but
  are not unit coverage, so a loose pattern over-counts.
* A CodeRabbit skip notice is a comment with a non-empty body. Counting
  comments therefore cannot distinguish "reviewed" from "declined to
  review", and the skip wording is not a closed set -- three variants are
  in the wild.
"""

from __future__ import annotations

import pytest

from scripts.check_pr_gated import (
    AGGREGATED_JOBS,
    context_name,
    is_concluded,
    is_real_review,
    missing_aggregated_jobs,
    required_contexts_present,
    unit_test_contexts,
)

# Captured from PR #1611's statusCheckRollup. The CodeRabbit entry is a
# StatusContext with NO `name` key; every other entry is a CheckRun.
REAL_STATUS_CONTEXT = {
    "__typename": "StatusContext",
    "context": "CodeRabbit",
    "startedAt": "2026-09-01T22:37:05Z",
    "state": "SUCCESS",
    "targetUrl": "",
}

REAL_CHECK_RUN = {
    "__typename": "CheckRun",
    "completedAt": "2026-09-01T22:33:32Z",
    "conclusion": "SKIPPED",
    "detailsUrl": "https://github.com/vitali87/code-graph-rag/actions/runs/33566887159/job/100052009082",
    "name": "scan-scheduled",
    "startedAt": "2026-09-01T22:33:32Z",
    "status": "COMPLETED",
    "workflowName": "OSV-Scanner",
}

# A queued CheckRun: conclusion is the empty string, not null.
REAL_QUEUED_CHECK_RUN = {
    "__typename": "CheckRun",
    "completedAt": "",
    "conclusion": "",
    "name": "Unit Tests (ubuntu-latest, py3.12)",
    "startedAt": "2026-09-01T22:33:32Z",
    "status": "QUEUED",
    "workflowName": "CI",
}

# Captured from PR #1576, live at the time of writing. A THIRD skip shape,
# beyond the two named in the issue: neither "auto reviews are disabled"
# nor "already reviewed".
REAL_RATE_LIMIT_NOTICE = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n"
    "\n> [!WARNING]\n> ## Review limit reached\n> \n"
    "> **Next included review available in 31 minutes.**\n"
)

REAL_AUTO_REVIEW_DISABLED_NOTICE = (
    "> [!IMPORTANT]\n> ## Review skipped\n>\n"
    "> Auto reviews are disabled on base/target branches other than the "
    "default branch.\n>\n> Please check the settings in the CodeRabbit UI "
    "or the `.coderabbit.yaml` file in this repository. To trigger a single "
    "review, invoke the `@coderabbitai review` command.\n>\n"
    "> Configuration used: **defaults**\n"
)

# Captured from PR #1596: a completed review that found nothing. This is a
# REAL review and must count, which is what makes "empty means skipped"
# wrong.
REAL_EMPTY_BUT_COMPLETED_REVIEW = (
    "**Actionable comments posted: 0**\n\n"
    "<details>\n<summary>♻️ Duplicate comments (1)</summary>\n"
    "</details>\n\n"
    "No actionable comments were generated in the recent review."
)


class TestContextName:
    """Both rollup shapes must yield a name, and neither may raise."""

    def test_a_check_run_uses_its_name(self) -> None:
        assert context_name(REAL_CHECK_RUN) == "scan-scheduled"

    def test_a_status_context_has_no_name_key_and_must_not_raise(self) -> None:
        assert "name" not in REAL_STATUS_CONTEXT
        assert context_name(REAL_STATUS_CONTEXT) == "CodeRabbit"

    def test_an_unknown_shape_yields_empty_rather_than_raising(self) -> None:
        """A silent skip is wrong, but so is dying mid-scan.

        Returning "" lets the caller report the entry as unnamed instead of
        aborting the whole check, which is how the jq form failed.
        """
        assert context_name({"__typename": "Mystery"}) == ""


class TestIsConcluded:
    def test_a_queued_check_is_not_concluded(self) -> None:
        """`conclusion` is "" here, not null: a `or "pending"` default lies."""
        assert REAL_QUEUED_CHECK_RUN["conclusion"] == ""
        assert is_concluded(REAL_QUEUED_CHECK_RUN) is False

    def test_a_completed_check_is_concluded(self) -> None:
        assert is_concluded(REAL_CHECK_RUN) is True

    def test_the_naive_none_default_would_have_passed_this(self) -> None:
        """Pins why the guard is written against "" and not against None."""
        assert (REAL_QUEUED_CHECK_RUN["conclusion"] or "pending") == "pending"
        assert REAL_QUEUED_CHECK_RUN.get("conclusion", "pending") == ""


class TestUnitTestContexts:
    def test_integration_and_smoke_are_not_unit_tests(self) -> None:
        rollup = [
            {"__typename": "CheckRun", "name": "Unit Tests (ubuntu-latest, py3.12)"},
            {"__typename": "CheckRun", "name": "Integration Tests (ubuntu-latest)"},
            {"__typename": "CheckRun", "name": "Binary Smoke Test"},
        ]

        names = unit_test_contexts(rollup)

        assert names == ["Unit Tests (ubuntu-latest, py3.12)"]

    def test_a_loose_match_would_have_counted_three(self) -> None:
        """The control that makes the assertion above mean something."""
        rollup = [
            {"__typename": "CheckRun", "name": "Unit Tests (ubuntu-latest, py3.12)"},
            {"__typename": "CheckRun", "name": "Integration Tests (ubuntu-latest)"},
            {"__typename": "CheckRun", "name": "Binary Smoke Test"},
        ]

        loose = [e for e in rollup if "test" in context_name(e).lower()]

        assert len(loose) == 3, "fixture no longer demonstrates the over-count"


class TestRequiredContextsPresent:
    def test_a_missing_context_is_reported_not_ignored(self) -> None:
        rollup = [{"__typename": "CheckRun", "name": "All Checks Pass"}]

        missing = required_contexts_present(rollup, ["All Checks Pass", "CodeRabbit"])

        assert missing == ["CodeRabbit"]

    def test_presence_is_not_inferred_from_absence_of_failure(self) -> None:
        """An empty rollup must report every required context missing.

        This is the #1582 shape: zero failures out of a set containing no
        tests reads as clean.
        """
        missing = required_contexts_present([], ["All Checks Pass"])

        assert missing == ["All Checks Pass"]


class TestMissingAggregatedJobs:
    def test_matrix_job_names_are_matched_by_prefix(self) -> None:
        """Exact comparison finds none of them and reports everything missing."""
        rollup = [
            {"__typename": "CheckRun", "name": f"{job} (ubuntu-latest, py3.12)"}
            for job in AGGREGATED_JOBS
        ]

        assert missing_aggregated_jobs(rollup) == []

    def test_the_codeql_only_set_reports_every_job_missing(self) -> None:
        """The #1582 shape: a green aggregate over a set containing no tests.

        Seven CodeQL contexts and nothing else is what a stacked branch is
        left with after a rebase cancels its runs, and `0 FAILURE` over that
        set reads as clean.
        """
        rollup = [
            {"__typename": "CheckRun", "name": "Analyze (actions)"},
            {"__typename": "CheckRun", "name": "Analyze (python)"},
            {"__typename": "StatusContext", "context": "CodeRabbit"},
        ]

        assert missing_aggregated_jobs(rollup) == list(AGGREGATED_JOBS)


class TestIsRealReview:
    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("rate limited", REAL_RATE_LIMIT_NOTICE),
            ("auto review disabled", REAL_AUTO_REVIEW_DISABLED_NOTICE),
        ],
    )
    def test_a_skip_notice_is_not_a_review(self, label: str, body: str) -> None:
        assert body.strip(), f"{label}: fixture is empty, so it proves nothing"
        assert is_real_review(body) is False, label

    def test_a_completed_review_that_found_nothing_still_counts(self) -> None:
        """The case that makes "non-empty body" and "no findings" both wrong.

        A review reporting zero actionable comments DID run. Treating it as
        skipped would refuse a properly reviewed PR.
        """
        assert is_real_review(REAL_EMPTY_BUT_COMPLETED_REVIEW) is True

    def test_an_empty_body_is_not_a_review(self) -> None:
        assert is_real_review("") is False
        assert is_real_review("   \n  ") is False

    def test_an_unrecognised_notice_shape_is_refused_not_admitted(self) -> None:
        """Fails closed on wording nobody has seen yet.

        The skip wording is not a closed set -- three variants are already
        in the wild, and #1581 was filed knowing only two. So the test is
        "does this positively carry a review verdict", not "is this absent
        from a blocklist of known skips"; a blocklist admits every future
        variant by default, which is the wrong direction to fail in.
        """
        invented_future_notice = (
            "> [!WARNING]\n> ## Review postponed\n> Some new reason nobody "
            "has written down yet.\n"
        )

        assert is_real_review(invented_future_notice) is False
