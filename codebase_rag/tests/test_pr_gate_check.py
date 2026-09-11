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

import json

import pytest

from scripts import check_pr_gated
from scripts.check_pr_gated import (
    AGGREGATED_JOBS,
    BLOCKED_VALIDATION_MARKERS,
    absent_context_reason,
    context_name,
    is_concluded,
    is_real_review,
    missing_aggregated_jobs,
    required_contexts_present,
    review_execution_caveats,
    unit_test_contexts,
    unresolved_in_page,
    validation_was_blocked,
)

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


REAL_QUEUED_CHECK_RUN = {
    "__typename": "CheckRun",
    "completedAt": "",
    "conclusion": "",
    "name": "Unit Tests (ubuntu-latest, py3.12)",
    "startedAt": "2026-09-01T22:33:32Z",
    "status": "QUEUED",
    "workflowName": "CI",
}


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

    def test_the_entry_a_name_only_scan_drops_is_the_review_bot(self) -> None:
        """The drop is not random: it lands on the review evidence.

        Verified independently on PR #1624, whose rollup is 28 `CheckRun`
        entries and exactly one `StatusContext` -- and that one is
        CodeRabbit. So a scan keyed on `name` reports the review bot ABSENT
        on a PR where it reviewed cleanly and anchored to the head, which
        fails in the safe-looking direction.

        The control is the naive scan itself, so the assertion below means
        something rather than restating the implementation.
        """
        rollup = [
            {"__typename": "CheckRun", "name": "Unit Tests (ubuntu-latest, py3.12)"},
            REAL_STATUS_CONTEXT,
        ]

        naive = [e.get("name") for e in rollup if e.get("name")]
        resolved = [context_name(e) for e in rollup]

        assert "CodeRabbit" not in naive, "fixture no longer shows the drop"
        assert "CodeRabbit" in resolved

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


class TestReviewEvidenceIsNotForgeable:
    """A verdict marker written by anyone must not satisfy the gate.

    Greptile's P1 on PR #1625: bodies were pooled without author, so the
    PR author could write "confidence score" in an ordinary comment and
    the gate would report the PR reviewed. The check then asserted
    something the author controls (CWE-345).
    """

    def test_the_pr_author_cannot_forge_a_verdict(self) -> None:
        forged = "Looks good. confidence score is fine here."

        assert is_real_review(forged, "vitali87") is False

    def test_the_same_body_from_the_bot_does_count(self) -> None:
        """The control: it is the AUTHOR that makes the difference, not the text.

        Without this the test above would also pass if `is_real_review`
        rejected the body for some unrelated reason.
        """
        forged = "Looks good. confidence score is fine here."

        assert is_real_review(forged, "coderabbitai") is True

    def test_a_trusted_author_alone_is_not_enough(self) -> None:
        """The bot posts the skip notices too, so author alone cannot decide."""
        assert is_real_review(REAL_RATE_LIMIT_NOTICE, "coderabbitai") is False

    def test_the_bot_suffix_spelling_is_accepted(self) -> None:
        """`author.login` and `user.login` differ on the `[bot]` suffix."""
        assert is_real_review(REAL_EMPTY_BUT_COMPLETED_REVIEW, "coderabbitai[bot]")

    def test_an_unknown_author_fails_closed(self) -> None:
        assert is_real_review(REAL_EMPTY_BUT_COMPLETED_REVIEW, "") is False


class TestUnresolvedInPage:
    """An unresolved thread on page two must not be invisible.

    `reviewThreads(first: 100)` without `pageInfo` silently truncates, and
    PR #1503 carried 53 threads, so the ceiling is reachable rather than
    theoretical (Greptile P1 on PR #1625).
    """

    @staticmethod
    def _page(unresolved: int, has_next: bool, cursor: str) -> dict[str, object]:
        nodes = [{"isResolved": False}] * unresolved
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": cursor,
                            },
                        }
                    }
                }
            }
        }

    def test_page_one_reports_that_another_page_exists(self) -> None:
        count, has_next, cursor = unresolved_in_page(self._page(0, True, "Y3Vyc29y"))

        assert count == 0
        assert has_next is True, "a single-page read would stop here and report clean"
        assert cursor == "Y3Vyc29y"

    def test_the_blocker_on_page_two_is_counted(self) -> None:
        count, has_next, _ = unresolved_in_page(self._page(1, False, ""))

        assert count == 1
        assert has_next is False

    def test_a_malformed_page_raises_rather_than_reporting_zero(self) -> None:
        """The caller turns this into "unverified", never into a clean count."""
        with pytest.raises((KeyError, TypeError)):
            unresolved_in_page({"data": {}})

    def test_a_page_with_nodes_but_no_pageinfo_is_unverified_not_final(self) -> None:
        """The one shape that used to answer "complete" instead of "unverified".

        `info = threads.get("pageInfo", {})` defaulted `hasNextPage` to
        False, so a page carrying `nodes` and no `pageInfo` key read as the
        FINAL page and returned its count with no error -- while every other
        unreadable shape here routes to unverified. A missing key answered
        as a clean result is the class this script exists to close, so the
        inconsistency mattered more than its likelihood (CodeRabbit on
        PR #1625).

        The two-page fixtures above are the control: they must stay green,
        or this would have been "fixed" by making every page unverified.
        """
        no_page_info = {
            "data": {
                "repository": {
                    "pullRequest": {"reviewThreads": {"nodes": [{"isResolved": False}]}}
                }
            }
        }

        with pytest.raises(KeyError):
            unresolved_in_page(no_page_info)

    def test_a_pageinfo_without_hasnextpage_is_unverified_not_final(self) -> None:
        """The same defect one level down, which the first fix missed.

        `bool(info.get("hasNextPage"))` is False for a `pageInfo` that exists
        but omits the field, so pagination ended as if complete. Fixing the
        missing-`pageInfo` case without this one repaired the named instance
        rather than the class (CodeRabbit on PR #1625).
        """
        missing_flag = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"isResolved": False}],
                            "pageInfo": {"endCursor": "Y3Vyc29y"},
                        }
                    }
                }
            }
        }

        with pytest.raises(KeyError):
            unresolved_in_page(missing_flag)

    def test_a_string_hasnextpage_is_refused_rather_than_trusted(self) -> None:
        """A wrong type must not be read as an answer.

        `"false"` is a truthy string, so a shape change turning the flag into
        a string would invert this check while looking like it worked.
        """
        wrong_type = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": "false", "endCursor": ""},
                        }
                    }
                }
            }
        }

        with pytest.raises(TypeError):
            unresolved_in_page(wrong_type)


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
        assert is_real_review(body, "coderabbitai") is False, label

    def test_a_completed_review_that_found_nothing_still_counts(self) -> None:
        """The case that makes "non-empty body" and "no findings" both wrong.

        A review reporting zero actionable comments DID run. Treating it as
        skipped would refuse a properly reviewed PR.
        """
        assert is_real_review(REAL_EMPTY_BUT_COMPLETED_REVIEW, "coderabbitai") is True

    def test_an_empty_body_is_not_a_review(self) -> None:
        assert is_real_review("", "coderabbitai") is False
        assert is_real_review("   \n  ", "coderabbitai") is False

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

        assert is_real_review(invented_future_notice, "coderabbitai") is False


class TestCiRunsAtHeadIsNotWindowed:
    """The run lookup must ask by SHA, not page recent runs.

    Measured on PR #1826: its CI run was 36 minutes old, all 40 of the most
    recent repo runs were newer, and the tool reported a fully green PR as
    having "no CI run at the head". A windowed lookup turns repo traffic
    into a false "never ran" verdict, which is the exact confusion this
    checker exists to remove.
    """

    HEAD = "8c93429b6e9bc17a61b3096c296cae1a26f1a411"

    CI_PATH = ".github/workflows/ci.yml"

    def _payload(self, *, path: str | None = None) -> str:
        return json.dumps(
            {
                "workflow_runs": [
                    {
                        "id": 34416630107,
                        "name": "CI",
                        "path": path if path is not None else self.CI_PATH,
                        "head_sha": self.HEAD,
                    }
                ]
            }
        )

    def test_the_query_is_scoped_to_the_head_sha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, ...]] = []

        def fake(*args: str) -> str:
            seen.append(args)
            return self._payload()

        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake)

        check_pr_gated.ci_runs_at_head(self.HEAD)

        assert any(f"head_sha={self.HEAD}" in arg for call in seen for arg in call)

    def test_the_request_paginates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`head_sha` is exact but not unbounded -- it pages at 30 by default.

        Without --paginate a run on a later page reads as "no run at
        head", which is the windowing bug this fix removes, returning at
        a larger size. The decoder test cannot catch this: it feeds
        pre-concatenated pages to a stub, so it covers the parsing but
        not the flag that makes multiple pages arrive.
        """
        seen: list[tuple[str, ...]] = []

        def fake(*args: str) -> str:
            seen.append(args)
            return self._payload()

        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake)

        check_pr_gated.ci_runs_at_head(self.HEAD)

        # Assert the ARGUMENT ORDER, not merely that the flag is present.
        # `gh --paginate api ...` is rejected by gh, and a presence-only
        # check passes for it -- caught exactly that way while restoring
        # this flag after a mutation.
        assert seen, "no gh call was made"
        call = seen[0]
        assert call[0] == "api"
        assert "--paginate" in call
        assert call.index("--paginate") > call.index("api")
        assert any("per_page=100" in arg for arg in call)

    def test_it_does_not_page_recent_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `--limit` listing is the bug; it must not be how the answer is got."""
        seen: list[tuple[str, ...]] = []

        def fake(*args: str) -> str:
            seen.append(args)
            return self._payload()

        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake)

        check_pr_gated.ci_runs_at_head(self.HEAD)

        assert not any("--limit" in call for call in seen)

    def test_a_run_older_than_any_window_is_still_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            check_pr_gated, "_gh_stdout_or_empty", lambda *a: self._payload()
        )

        runs = check_pr_gated.ci_runs_at_head(self.HEAD)

        assert [r["id"] for r in runs] == [34416630107]

    def test_a_non_ci_workflow_at_the_same_sha_is_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            check_pr_gated,
            "_gh_stdout_or_empty",
            lambda *a: self._payload(path=".github/workflows/codeql.yml"),
        )

        assert check_pr_gated.ci_runs_at_head(self.HEAD) == []

    def test_an_impostor_workflow_merely_named_ci_is_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A display name is a string any workflow file may declare.

        Matching on it would let a second file called `CI` satisfy the
        check without running a single test, so the run is identified by
        the workflow PATH. The repo's own require-ci-at-head workflow
        matches on path for this reason.
        """
        monkeypatch.setattr(
            check_pr_gated,
            "_gh_stdout_or_empty",
            lambda *a: self._payload(path=".github/workflows/not-really-ci.yml"),
        )

        assert check_pr_gated.ci_runs_at_head(self.HEAD) == []

    def test_a_run_on_a_later_page_is_still_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--paginate` concatenates one JSON object per page.

        Decoding only the first would reintroduce the windowing bug at a
        larger size: a match on page two would read as "no run at head".
        """
        page_one = json.dumps(
            {
                "workflow_runs": [
                    {"id": 1, "name": "OSV", "path": ".github/workflows/osv.yml"}
                ]
            }
        )
        monkeypatch.setattr(
            check_pr_gated,
            "_gh_stdout_or_empty",
            lambda *a: page_one + "\n" + self._payload(),
        )

        runs = check_pr_gated.ci_runs_at_head(self.HEAD)

        assert [r["id"] for r in runs] == [34416630107]

    def test_an_unreachable_api_reports_no_runs_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", lambda *a: "")

        assert check_pr_gated.ci_runs_at_head(self.HEAD) == []


class TestCheckResolvesRunOwnershipWithTheRestField:
    """`check` must read the run id by the name the REST payload uses.

    `ci_runs_at_head` returns REST `workflow_runs` entries, which carry
    `id`. The previous lookup returned `gh run list` entries, which carry
    `databaseId`. Keeping the old name fetches `actions/runs/None`, leaves
    `owners` empty, and reports "does not resolve to #<pr>" -- trading one
    false blocker for another. Verified against live PR #1826.
    """

    def test_ownership_resolves_rather_than_reporting_an_empty_owner_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head = "f" * 40
        view = {
            "headRefOid": head,
            "baseRefName": "main",
            "statusCheckRollup": [],
            "comments": [],
            "reviews": [],
        }

        def fake(*args: str) -> str:
            if args[:2] == ("pr", "view"):
                return json.dumps(view)
            if args[0] == "api" and f"head_sha={head}" in " ".join(args):
                return json.dumps(
                    {
                        "workflow_runs": [
                            {
                                "id": 555,
                                "name": "CI",
                                "path": ".github/workflows/ci.yml",
                                "head_sha": head,
                            }
                        ]
                    }
                )
            if args[0] == "api" and args[1].endswith("/actions/runs/555"):
                return json.dumps({"pull_requests": [{"number": 1826}]})
            return ""

        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake)

        reasons = check_pr_gated.check("1826")

        assert not any("does not resolve to" in r for r in reasons)


class TestCiRunsAtHeadFailsClosedOnMalformedPages:
    """Malformed paginated output must yield fewer runs, never more.

    Every degradation here reports "no CI run at the head", which blocks
    a merge. The opposite direction -- inventing a run from unparseable
    output -- would report a PR gated on evidence that does not exist.
    """

    CI_PATH = ".github/workflows/ci.yml"

    def _page(self, run_id: int = 1) -> str:
        return json.dumps({"workflow_runs": [{"id": run_id, "path": self.CI_PATH}]})

    def _runs(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> list[dict]:
        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", lambda *a: raw)
        return check_pr_gated.ci_runs_at_head("a" * 40)

    @pytest.mark.parametrize(
        "label,raw,expected",
        [
            ("empty", "", 0),
            ("whitespace only", "   \n  ", 0),
            ("one page", None, 1),
            ("trailing whitespace", None, 1),
            ("page is null", None, 1),
            ("page is a list", None, 1),
            ("workflow_runs missing", None, 1),
            ("workflow_runs not a list", None, 1),
        ],
    )
    def test_malformed_output_never_invents_a_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        raw: str | None,
        expected: int,
    ) -> None:
        bodies = {
            "one page": self._page(),
            "trailing whitespace": self._page() + "\n\n  ",
            "page is null": "null\n" + self._page(),
            "page is a list": "[1,2]\n" + self._page(),
            "workflow_runs missing": '{"total_count":0}' + "\n" + self._page(),
            "workflow_runs not a list": '{"workflow_runs":"x"}' + "\n" + self._page(),
        }
        body = bodies[label] if raw is None else raw

        assert len(self._runs(monkeypatch, body)) == expected

    @pytest.mark.parametrize(
        "prefix", ["\n", " ", "\t\n ", "\r\n"], ids=["nl", "space", "mixed", "crlf"]
    )
    def test_leading_whitespace_does_not_discard_every_page(
        self, monkeypatch: pytest.MonkeyPatch, prefix: str
    ) -> None:
        """`raw_decode` does not tolerate leading whitespace.

        Skipping separators only AFTER a decode means a response that
        opens with one raises on the first pass and returns nothing,
        reported as "no CI run exists at the head SHA" -- the very
        verdict this lookup was rewritten to stop producing falsely.
        """
        runs = self._runs(monkeypatch, prefix + self._page())

        assert [r["id"] for r in runs] == [1]

    def test_leading_whitespace_before_multiple_pages_keeps_them_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = "\n" + self._page(1) + "\n" + self._page(2)

        assert [r["id"] for r in self._runs(monkeypatch, raw)] == [1, 2]

    def test_a_truncated_final_page_keeps_the_pages_already_decoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cut-off last page must not discard the complete ones before it."""
        raw = self._page() + "\n" + '{"workflow_runs":[{"id":2,'

        assert [r["id"] for r in self._runs(monkeypatch, raw)] == [1]

    def test_the_page_loop_terminates_on_unparseable_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards against an infinite loop when the decoder cannot advance."""
        assert self._runs(monkeypatch, "not json" + self._page()) == []


class TestTheTrueNegativeSurvivesTheFix:
    """Removing the false "no CI run" must not weaken the real one.

    The point of this gate is to refuse a PR whose CI never ran, so a fix
    aimed at a false negative has to be checked against the true one --
    otherwise it trades a tool that cries wolf for one that waves
    everything through.
    """

    HEAD = "a" * 40

    def test_a_head_with_no_runs_at_all_reports_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            check_pr_gated,
            "_gh_stdout_or_empty",
            lambda *a: json.dumps({"total_count": 0, "workflow_runs": []}),
        )

        assert check_pr_gated.ci_runs_at_head(self.HEAD) == []

    def test_a_head_whose_runs_are_all_other_workflows_reports_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CodeQL and OSV run without CI on a queued head -- a real case.

        Observed on #1847: every other workflow had reported while
        `ci.yml` was still pending, so the head carried runs but none of
        them was the one the gate requires.
        """
        monkeypatch.setattr(
            check_pr_gated,
            "_gh_stdout_or_empty",
            lambda *a: json.dumps(
                {
                    "workflow_runs": [
                        {"id": 1, "path": ".github/workflows/codeql.yml"},
                        {"id": 2, "path": ".github/workflows/osv-scanner.yml"},
                    ]
                }
            ),
        )

        assert check_pr_gated.ci_runs_at_head(self.HEAD) == []


REAL_REVIEW_WITH_BLOCKED_VALIDATION = (
    "## Confidence score: 3/5\n\n"
    "Last reviewed commit: 7d3c49e5\n\n"
    "- A standalone rollback harness was attempted, but production imports "
    "could not start because prompt_toolkit was absent in the incomplete "
    "environment.\n"
    "- The test suite remains blocked in this environment and historic paths "
    "could not be behaviorally disproved; no precise present bug can be "
    "claimed without a runnable import/test environment.\n"
)


REAL_REVIEW_BLOCKED_BY_IMPORT_ERROR = (
    "## Confidence score: 4/5\n\n"
    "Both commands failed during import with ModuleNotFoundError: No module "
    "named 'loguru' before _updater_for_reingest() could run; dependency "
    "installation is blocked because building pymgclient requires CMake.\n"
)


class TestValidationWasBlocked:
    """A review that could not RUN reads identically to one that verified.

    #1824: the blocked-validation note lands in a collapsed log section
    that a merge gate never opens and a human skims past. Detecting it is
    the difference between "the bot checked this" and "the bot reasoned
    about this and said so".
    """

    def test_the_real_blocked_review_is_detected(self) -> None:
        assert validation_was_blocked(REAL_REVIEW_WITH_BLOCKED_VALIDATION) is True

    def test_the_import_error_wording_is_deliberately_NOT_detected(self) -> None:
        """A KNOWN MISS, accepted on purpose.

        This artifact's phrasings ("failed during import with
        ModuleNotFoundError", "dependency installation is blocked") are
        exactly as plausible in a finding about the reviewed code: a
        plugin that fails to import, an installer that blocks. Detecting
        it would flag executed reviews as unexecuted, which discredits
        work that was done -- a worse error than staying silent.

        The blocklist fails permissive by design, so a miss degrades to
        today's behaviour. If this artifact needs catching, the fix is a
        reviewer-validation SECTION to parse, not a broader substring.
        """
        assert validation_was_blocked(REAL_REVIEW_BLOCKED_BY_IMPORT_ERROR) is False

    def test_a_review_that_ran_is_not_flagged(self) -> None:
        """The negative case. Without this the detector could return True
        for everything and every test above would still pass."""
        assert validation_was_blocked(REAL_EMPTY_BUT_COMPLETED_REVIEW) is False

    def test_an_empty_body_is_not_flagged(self) -> None:
        assert validation_was_blocked("") is False
        assert validation_was_blocked("   \n ") is False

    def test_a_blocked_review_is_still_a_real_review(self) -> None:
        """Blocked validation must NOT disqualify the artifact.

        The finding in #1547's blocked review turned out to be correct and
        was fixed. Unverified is not wrong, so this is a caveat on the
        evidence, never a reason to refuse the PR -- and non-execution is
        legitimate anyway when a PR has no Python surface to exercise.
        """
        assert (
            is_real_review(REAL_REVIEW_WITH_BLOCKED_VALIDATION, "greptile-apps") is True
        )


class TestReviewExecutionCaveats:
    """The WIRING, not the detector.

    Without these, deleting the caveat call from `check` leaves every
    other test in this file green -- coverage that cannot fail for the
    reason it exists (found by mutating the call site, not by reading).
    """

    def test_a_blocked_review_produces_a_caveat(self) -> None:
        caveats = review_execution_caveats(
            [(REAL_REVIEW_WITH_BLOCKED_VALIDATION, "greptile-apps")]
        )

        assert len(caveats) == 1
        assert "could not execute" in caveats[0]

    def test_a_review_that_ran_produces_none(self) -> None:
        assert (
            review_execution_caveats(
                [(REAL_EMPTY_BUT_COMPLETED_REVIEW, "coderabbitai")]
            )
            == []
        )

    def test_no_reviews_produces_none(self) -> None:
        assert review_execution_caveats([]) == []

    def test_all_reviewers_blocked_says_so(self) -> None:
        """Distinct wording from the partial case: if EVERY review was
        blocked there is no executed second opinion to fall back on."""
        caveats = review_execution_caveats(
            [
                (REAL_REVIEW_WITH_BLOCKED_VALIDATION, "greptile-apps"),
                (
                    "Confidence score: 3/5. The findings could not be "
                    "behaviorally disproved in this environment.",
                    "coderabbitai",
                ),
            ]
        )

        assert len(caveats) == 1
        assert caveats[0].startswith("every review artifact present")

    def test_one_blocked_among_several_is_the_partial_case(self) -> None:
        caveats = review_execution_caveats(
            [
                (REAL_REVIEW_WITH_BLOCKED_VALIDATION, "greptile-apps"),
                (REAL_EMPTY_BUT_COMPLETED_REVIEW, "coderabbitai"),
            ]
        )

        assert len(caveats) == 1
        assert caveats[0].startswith("a review by greptile-apps")


class TestCheckSurfacesTheCaveat:
    """`check` itself must consult the caveat, not merely be able to.

    The class above tests the helper in isolation and stays green when
    the call site is deleted -- the exact "two guards, remove either and
    it is still green" shape. This one stubs the only I/O seam
    (`_gh_stdout_or_empty`) and asserts on `check`'s own return value, so
    removing the call from `check` reddens it.
    """

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, review_body: str) -> None:
        view = {
            "headRefOid": "d" * 40,
            "baseRefName": "main",
            "statusCheckRollup": [],
            "comments": [{"body": review_body, "author": {"login": "greptile-apps"}}],
            "reviews": [],
        }

        def fake(*args: str) -> str:
            if args[:2] == ("pr", "view"):
                return json.dumps(view)
            return ""

        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake)

    def test_check_reports_the_caveat_for_a_blocked_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, REAL_REVIEW_WITH_BLOCKED_VALIDATION)

        _, caveats = check_pr_gated.check("1547")

        assert any("could not execute" in c for c in caveats)

    def test_check_reports_no_caveat_for_a_review_that_ran(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, REAL_EMPTY_BUT_COMPLETED_REVIEW)

        _, caveats = check_pr_gated.check("1547")

        assert caveats == []

    def test_a_blocked_review_is_never_a_blocking_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core contract: unverified is not wrong, so it must not
        appear among the reasons that refuse a PR."""
        self._stub(monkeypatch, REAL_REVIEW_WITH_BLOCKED_VALIDATION)

        reasons, _ = check_pr_gated.check("1547")

        assert not any("could not execute" in r for r in reasons)


REAL_REVIEW_DESCRIBING_A_CRASH = (
    "## Confidence score: 4/5\n\n"
    "Last reviewed commit: abc1234\n\n"
    "- The server could not start when the config key is absent; it raises "
    "KeyError before binding.\n"
    "- The worker could not run the queued task, and users get a raw "
    "ModuleNotFoundError traceback.\n"
    "- The plugin failed during import when the entry point is misspelled.\n"
)


class TestMarkersDoNotMatchBugDescriptions:
    """The false-positive direction, which nothing else covers.

    Bare substrings like "could not start" match a finding DESCRIBING a
    crash just as readily as a reviewer describing its own broken
    environment. Flagging an executed review as unexecuted is worse than
    staying silent, so every marker must name the reviewer's environment.
    """

    def test_a_review_describing_a_crash_is_not_flagged(self) -> None:
        assert validation_was_blocked(REAL_REVIEW_DESCRIBING_A_CRASH) is False

    def test_the_motivating_artifact_is_still_caught(self) -> None:
        """Narrowing must not cost detection on the artifact #1824 was
        filed against, whose note names the environment explicitly."""
        assert validation_was_blocked(REAL_REVIEW_WITH_BLOCKED_VALIDATION) is True

    def test_no_marker_subsumes_another(self) -> None:
        """A marker that is a superstring of another can never be the one
        that matches, so it is dead configuration that reads as coverage."""
        dead = [
            longer
            for longer in BLOCKED_VALIDATION_MARKERS
            for shorter in BLOCKED_VALIDATION_MARKERS
            if longer != shorter and shorter in longer
        ]

        assert dead == []

    def test_one_blocked_artifact_among_an_authors_own_is_partial(self) -> None:
        """Greptile re-scores in place and posts repeatedly, so the same
        author routinely has both a blocked and an executed artifact.
        Counting distinct AUTHORS called that "every review blocked"."""
        caveats = review_execution_caveats(
            [
                (REAL_REVIEW_WITH_BLOCKED_VALIDATION, "greptile-apps"),
                (REAL_REVIEW_DESCRIBING_A_CRASH, "greptile-apps"),
            ]
        )

        assert len(caveats) == 1
        assert caveats[0].startswith("a review by greptile-apps")

    def test_every_marker_is_load_bearing(self) -> None:
        """Each marker must be the SOLE reason some real phrasing is
        caught. Without this, any one could be deleted with the suite
        green -- the fixtures match several markers each, so they cannot
        distinguish a marker that works from one nobody needs.
        """
        sole_evidence = {
            "blocked in this environment": (
                "The test suite remains blocked in this environment."
            ),
            "could not be behaviorally disproved": (
                "Historic paths could not be behaviorally disproved."
            ),
            "without a runnable import/test environment": (
                "No bug can be claimed without a runnable import/test environment."
            ),
        }

        assert set(sole_evidence) == set(BLOCKED_VALIDATION_MARKERS)
        for marker, phrasing in sole_evidence.items():
            assert validation_was_blocked(phrasing) is True, marker
            others = tuple(m for m in BLOCKED_VALIDATION_MARKERS if m != marker)
            assert not any(m in phrasing.lower() for m in others), marker

    def test_an_executed_review_describing_these_defects_is_not_flagged(
        self,
    ) -> None:
        """The false positives that forced the third narrowing.

        Each of these is an EXECUTED review reporting a defect in the
        reviewed application, using wording an earlier marker matched.
        """
        executed_findings = (
            "I ran the full suite. The plugin failed during import with "
            "ModuleNotFoundError when the entry point is misspelled.",
            "Ran the installer end to end. When the lockfile is stale, "
            "dependency installation is blocked and the CLI exits 0 anyway.",
            "Executed the test suite. When the schema key is absent, "
            "validation blocked the request but the error message is empty.",
        )

        for finding in executed_findings:
            assert validation_was_blocked(finding) is False, finding


class TestEveryCheckReturnPathIsATuple:
    """`check` returns `tuple[list[str], list[str]]`, and one path did not.

    Found by CodeRabbit on the PR that widened the signature: the
    unreadable-PR early return still handed back a bare list, so `main`
    raised `ValueError: not enough values to unpack` at exactly the
    moment the tool exists to report -- `gh` being unusable. The
    annotation does not catch it because nothing type-checks this script
    in CI, and no test reached that branch.
    """

    @staticmethod
    def _gh_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
        """Every `gh` call returns empty, as it does when auth fails."""
        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", lambda *args: "")

    def test_an_unreadable_pr_returns_the_two_lists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._gh_is_broken(monkeypatch)

        result = check_pr_gated.check("9999")

        assert isinstance(result, tuple)
        reasons, caveats = result
        assert any("could not read PR #9999" in r for r in reasons)
        assert caveats == []

    def test_main_reports_the_failure_instead_of_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user-visible symptom: a traceback rather than a verdict.

        Asserting on `main` and not just on `check` is the point -- the
        bare list only becomes a crash at the unpacking call site, so a
        test that stops at `check`'s return value cannot see it.
        """
        self._gh_is_broken(monkeypatch)

        assert check_pr_gated.main(["check_pr_gated.py", "9999"]) == 1


# Captured from #1547 at 41ad9750 on 2026-09-10, the run that motivated
# #1827: `CI Ran At Head` had passed and twenty jobs were mid-flight, and
# the tool reported the same sentence it prints when nothing ran at all.
REAL_ROLLUP_MID_RUN = [
    {"__typename": "CheckRun", "name": "CI Ran At Head", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "Analyze (python)", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "Lint & Format", "conclusion": ""},
    {"__typename": "CheckRun", "name": "Type Check", "conclusion": ""},
    {
        "__typename": "CheckRun",
        "name": "Unit Tests (ubuntu-latest, py3.13)",
        "conclusion": "",
    },
]

# The #1582 shape: everything concluded, the aggregate never appeared.
REAL_ROLLUP_ALL_CONCLUDED = [
    {"__typename": "CheckRun", "name": "CodeQL", "conclusion": "SKIPPED"},
    {"__typename": "CheckRun", "name": "Analyze (actions)", "conclusion": "SUCCESS"},
]


class TestAbsentContextReason:
    """ "Absent" hides three states, two of which need opposite action.

    `All Checks Pass` is an aggregate that reports only once its
    dependencies finish, so it is legitimately missing for a whole run.
    The same sentence also covers #1582, where every job concluded and it
    never arrived. One means wait; the other means investigate.
    """

    def test_mid_run_says_the_checks_are_still_coming(self) -> None:
        reason = absent_context_reason("All Checks Pass", REAL_ROLLUP_MID_RUN)

        assert "has not reported YET" in reason
        assert "3 check(s)" in reason

    def test_mid_run_names_the_checks_still_running(self) -> None:
        """A count alone leaves the reader to go and look anyway."""
        reason = absent_context_reason("All Checks Pass", REAL_ROLLUP_MID_RUN)

        assert "Lint & Format" in reason

    def test_mid_run_does_not_name_a_finished_check_as_pending(self) -> None:
        reason = absent_context_reason("All Checks Pass", REAL_ROLLUP_MID_RUN)

        assert "CI Ran At Head" not in reason

    def test_all_concluded_says_it_is_never_arriving(self) -> None:
        reason = absent_context_reason("All Checks Pass", REAL_ROLLUP_ALL_CONCLUDED)

        assert "not going to appear" in reason
        assert "YET" not in reason

    def test_an_empty_rollup_says_nothing_ran(self) -> None:
        reason = absent_context_reason("All Checks Pass", [])

        assert "no check reported at the head at all" in reason

    def test_the_three_states_are_mutually_distinguishable(self) -> None:
        """The whole point is that a reader can tell them apart.

        Asserting each in isolation would pass if two returned the same
        sentence, which is precisely the defect being fixed.
        """
        said = {
            absent_context_reason("All Checks Pass", REAL_ROLLUP_MID_RUN),
            absent_context_reason("All Checks Pass", REAL_ROLLUP_ALL_CONCLUDED),
            absent_context_reason("All Checks Pass", []),
        }

        assert len(said) == 3

    def test_a_queued_check_counts_as_pending(self) -> None:
        """A queued check reports `conclusion: ""`, an empty STRING.

        Read as concluded, a fully-queued run reports as "not going to
        appear" -- the alarming wording, for the most ordinary state.
        """
        queued = [{"__typename": "CheckRun", "name": "Type Check", "conclusion": ""}]

        assert "has not reported YET" in absent_context_reason("X", queued)


class TestCheckDistinguishesPendingFromNeverRan:
    """`check` must consult the helper, not merely be able to.

    The class above stays green when the call site is deleted. This one
    stubs the I/O seam and asserts on `check`'s own return, so removing
    the call reddens it.
    """

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, rollup: list[dict[str, object]]) -> None:
        view = {
            "headRefOid": "e" * 40,
            "baseRefName": "main",
            "statusCheckRollup": rollup,
            "comments": [],
            "reviews": [],
        }

        def fake(*args: str) -> str:
            if args[:2] == ("pr", "view"):
                return json.dumps(view)
            return ""

        monkeypatch.setattr(check_pr_gated, "_gh_stdout_or_empty", fake)

    def test_check_says_in_flight_for_a_mid_run_pr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, REAL_ROLLUP_MID_RUN)

        reasons, _ = check_pr_gated.check("1547")

        assert any("has not reported YET" in r for r in reasons)

    def test_check_does_not_say_in_flight_once_everything_concluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, REAL_ROLLUP_ALL_CONCLUDED)

        reasons, _ = check_pr_gated.check("1582")

        assert not any("has not reported YET" in r for r in reasons)
        assert any("not going to appear" in r for r in reasons)

    def test_a_mid_run_pr_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wording only. A PR mid-run is not verifiably gated, and the
        tool is right to refuse it -- the defect was that it could not say
        why, not that it refused.
        """
        self._stub(monkeypatch, REAL_ROLLUP_MID_RUN)

        reasons, _ = check_pr_gated.check("1547")

        assert any("required context absent at the head" in r for r in reasons)


class TestEntryFinishedHandlesBothRollupShapes:
    """A `StatusContext` carries `state`, never `conclusion`.

    Judged by `is_concluded` alone, every third-party status is unfinished
    forever, so a rollup containing one can never reach the all-concluded
    branch: #1582 then reports as "still running, re-check" -- the
    reassuring reading, in the one case that needs investigating. The
    older `is_concluded` call site filters to a name only a `CheckRun`
    ever has, which is why the gap stayed latent.
    """

    def test_a_finished_status_context_is_finished(self) -> None:
        assert check_pr_gated.entry_finished(REAL_STATUS_CONTEXT) is True

    def test_a_pending_status_context_is_not_finished(self) -> None:
        pending = {"__typename": "StatusContext", "context": "X", "state": "PENDING"}

        assert check_pr_gated.entry_finished(pending) is False

    def test_a_queued_check_run_is_not_finished(self) -> None:
        """The empty-STRING conclusion trap, still handled."""
        queued = {"__typename": "CheckRun", "name": "X", "conclusion": ""}

        assert check_pr_gated.entry_finished(queued) is False

    def test_a_finished_status_context_does_not_block_the_verdict(self) -> None:
        """The bug this predicate exists for, at the level that matters.

        With the real captured fixture in an otherwise-concluded rollup,
        the all-concluded branch must still be reachable.
        """
        rollup = [*REAL_ROLLUP_ALL_CONCLUDED, REAL_STATUS_CONTEXT]

        reason = absent_context_reason("All Checks Pass", rollup)

        assert "not going to appear" in reason
        assert "YET" not in reason


class TestOnlyDependenciesExplainAnAbsentAggregate:
    """An unrelated pending check must not suppress "investigate".

    `All Checks Pass` waits on AGGREGATED_JOBS and nothing else, so only
    those being unfinished can explain its absence. Counting every
    unfinished entry meant one unrelated pending check flipped the verdict
    from "investigate" to "wait" -- and CodeRabbit is pending on nearly
    every PR here, so the wrong branch was the common case. Reported by
    Greptile on #1831.
    """

    @staticmethod
    def _concluded() -> list[dict[str, object]]:
        return [
            {
                "__typename": "CheckRun",
                "name": job,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
            for job in AGGREGATED_JOBS
        ]

    @staticmethod
    def _pending(name: str) -> dict[str, object]:
        return {
            "__typename": "CheckRun",
            "name": name,
            "status": "IN_PROGRESS",
            "conclusion": "",
        }

    def test_an_unrelated_pending_check_does_not_say_wait(self) -> None:
        rollup = [*self._concluded(), self._pending("CodeRabbit")]

        reason = absent_context_reason("All Checks Pass", rollup)

        assert "re-check rather than investigate" not in reason
        assert "not going to appear" in reason

    def test_a_pending_dependency_still_says_wait(self) -> None:
        rollup = [*self._concluded()[:-1], self._pending("Binary Smoke Test")]

        reason = absent_context_reason("All Checks Pass", rollup)

        assert "re-check rather than investigate" in reason

    def test_a_pending_matrix_dependency_is_matched_by_prefix(self) -> None:
        """Matrix jobs carry a platform suffix, so exact matching finds none."""
        rollup = [
            *self._concluded()[:-1],
            self._pending("Unit Tests (ubuntu-latest, py3.12)"),
        ]

        reason = absent_context_reason("All Checks Pass", rollup)

        assert "re-check rather than investigate" in reason

    def test_a_pending_dependency_wins_over_unrelated_noise(self) -> None:
        rollup = [
            *self._concluded()[:-1],
            self._pending("Binary Smoke Test"),
            self._pending("CodeRabbit"),
        ]

        reason = absent_context_reason("All Checks Pass", rollup)

        assert "re-check rather than investigate" in reason
        assert "Binary Smoke Test" in reason

    def test_the_count_names_only_dependencies(self) -> None:
        """The number must not include checks the aggregate does not await."""
        rollup = [
            *self._concluded()[:-1],
            self._pending("Binary Smoke Test"),
            self._pending("CodeRabbit"),
            self._pending("Fuzz (address)"),
        ]

        reason = absent_context_reason("All Checks Pass", rollup)

        assert "1 check(s)" in reason


class TestPendingNamesReadHonestly:
    """The parenthetical must not claim names it does not have."""

    def test_no_ellipsis_when_every_pending_check_is_named(self) -> None:
        one = [{"__typename": "CheckRun", "name": "Type Check", "conclusion": ""}]

        assert "(Type Check)" in absent_context_reason("X", one)

    def test_ellipsis_only_once_names_are_omitted(self) -> None:
        """Names must be jobs the aggregate waits on, or they are filtered.

        Synthetic names (`Check 0`) no longer reach the parenthetical:
        only unfinished AGGREGATED_JOBS entries can explain the
        aggregate's absence, so the fixture uses four real ones.
        """
        four = [
            {"__typename": "CheckRun", "name": name, "conclusion": ""}
            for name in AGGREGATED_JOBS[:4]
        ]

        assert "..." in absent_context_reason("X", four)

    def test_no_empty_parentheses_when_no_name_is_known(self) -> None:
        """A dependency-shaped entry whose name cannot be read.

        `context_name` returns "" for a shape carrying neither `name` nor
        `context`, so such an entry is filtered out with the unrelated
        ones and cannot produce an empty parenthetical.
        """
        nameless = [{"conclusion": ""}, {"conclusion": ""}]

        reason = absent_context_reason("X", nameless)

        assert "()" not in reason
        assert "still running" not in reason
