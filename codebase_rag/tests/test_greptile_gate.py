# Greptile scored a pull request 3/5 with an open security finding and every
# check stayed green, because nothing read the score. The gate now fails when
# Greptile scores the exact head commit below 5/5, and only then: Greptile
# absent (out of credits, paused) or not scoring passes with a warning.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import greptile_gate as gg

_REPO = Path(__file__).resolve().parents[2]
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_HEAD = "5218d78ab933e8d1deb58e018f96c9b9390831a7"
_OLD = "86db96085b4a0a1b6b9409d9399de056f36d6b79"
# Well after every fixture timestamp, so a closed run has settled.
_NOW = datetime(2026, 11, 1, 18, tzinfo=UTC)


def _summary(sha: str, score: int | None, reviews: int = 1) -> str:
    # The shape Greptile posts on this repository, trimmed.
    marker = "" if score is None else f"<!-- greptile_confidence_score:{score} -->"
    heading = "" if score is None else f"Confidence Score: {score}/5"
    return (
        "<!-- greptile_summary -->\n\n"
        f'<h2><a href="https://app.greptile.com/x">View</a>{heading}</h2>\n\n'
        "<h3>Summary</h3>\n\nThis PR adds safeguards.\n\n"
        f"{marker}\n\n"
        f'<sub>Reviews ({reviews}) · Last reviewed commit: ["fix(security)!: '
        f'harden the MCP server&#39;s ..."](https://github.com/vitali87/'
        f"code-graph-rag/commit/{sha}) · [Reviewed by Greptile]"
        "(https://www.greptile.com/)</sub>"
    )


def _comment(
    body: str,
    login: str = gg.GREPTILE_LOGIN,
    updated: str = "2026-11-01T14:00:00Z",
) -> dict[str, Any]:
    return {
        "body": body,
        "user": {"login": login},
        "html_url": "https://github.com/vitali87/code-graph-rag/pull/3557#c",
        "created_at": "2026-11-01T13:59:21Z",
        "updated_at": updated,
    }


def _run(
    status: str = gg.COMPLETED,
    started: str | None = "2026-11-01T13:55:50Z",
    slug: str = gg.GREPTILE_APP,
    completed: str | None = None,
) -> dict[str, Any]:
    if completed is None and status == gg.COMPLETED and started:
        completed = started.replace("13:55:50", "13:59:18")
    return {
        "name": gg.REVIEW_CHECK,
        "status": status,
        "started_at": started,
        "completed_at": completed,
        "app": {"slug": slug},
    }


def test_a_5_of_5_review_of_the_head_passes() -> None:
    result = gg.verdict([_comment(_summary(_HEAD, 5))], _HEAD)
    assert result.state == gg.PASS
    assert "5/5" in result.message


@pytest.mark.parametrize("low", [0, 1, 2, 3, 4])
def test_a_review_of_the_head_below_5_fails(low: int) -> None:
    result = gg.verdict([_comment(_summary(_HEAD, low))], _HEAD)
    assert result.state == gg.FAIL
    assert f"{low}/5" in result.message
    assert "5/5 is required" in result.message


def test_the_score_is_read_from_the_heading_when_the_marker_is_gone() -> None:
    body = _summary(_HEAD, 3).replace("<!-- greptile_confidence_score:3 -->", "")
    assert gg.verdict([_comment(body)], _HEAD).state == gg.FAIL


def test_a_review_of_the_head_without_a_score_does_not_apply() -> None:
    # The gate applies only when Greptile scores.
    result = gg.verdict([_comment(_summary(_HEAD, None))], _HEAD)
    assert result.state == gg.SKIP
    assert "no confidence score" in result.message


def test_a_5_of_5_review_of_an_older_commit_does_not_pass() -> None:
    # A score of the code before the last push says nothing about the head.
    result = gg.verdict([_comment(_summary(_OLD, 5))], _HEAD)
    assert result.state == gg.ABSENT
    assert _OLD[:8] in result.message


def test_a_3_of_5_review_of_an_older_commit_does_not_fail() -> None:
    assert gg.verdict([_comment(_summary(_OLD, 3))], _HEAD).state == gg.ABSENT


def test_no_greptile_review_is_absent() -> None:
    assert gg.verdict([], _HEAD).state == gg.ABSENT


def test_the_trex_progress_comment_is_not_a_review() -> None:
    trex = "<!-- greptile_trex_summary -->\n\nTREX is testing this pull request"
    assert gg.verdict([_comment(trex)], _HEAD).state == gg.ABSENT


@pytest.mark.parametrize("login", ["vitali87", "greptile-apps", "greptileai"])
def test_a_score_written_by_anyone_but_greptiles_app_is_ignored(login: str) -> None:
    # The marker is plain text: the author could post a 5/5 of their own.
    forged = _comment(_summary(_HEAD, 5), login=login)
    assert gg.verdict([forged], _HEAD).state == gg.ABSENT


def test_a_forged_5_does_not_outvote_greptiles_3() -> None:
    comments = [
        _comment(_summary(_HEAD, 3)),
        _comment(_summary(_HEAD, 5), login="vitali87", updated="2026-11-02T00:00Z"),
    ]
    assert gg.verdict(comments, _HEAD).state == gg.FAIL


def test_the_newest_review_of_the_head_decides() -> None:
    comments = [
        _comment(_summary(_HEAD, 5, reviews=2), updated="2026-11-01T16:00:00Z"),
        _comment(_summary(_HEAD, 3, reviews=1), updated="2026-11-01T15:00:00Z"),
    ]
    assert gg.verdict(comments, _HEAD).state == gg.PASS


def test_an_uppercase_head_matches() -> None:
    assert gg.verdict([_comment(_summary(_HEAD, 5))], _HEAD.upper()).state == gg.PASS


def test_a_finished_review_run_lets_the_score_stand() -> None:
    # The run that produced the summary started before the summary was edited.
    result = gg.verdict([_comment(_summary(_HEAD, 3))], _HEAD, [_run()], _NOW)
    assert result.state == gg.FAIL


def test_an_open_review_run_waits_instead_of_failing_on_the_old_score() -> None:
    # `@greptile review` without a push: the summary still holds the old 3/5
    # of this very commit while Greptile re-scores it.
    runs = [_run(), _run(status="in_progress", started="2026-11-01T15:00:00Z")]
    result = gg.verdict([_comment(_summary(_HEAD, 3))], _HEAD, runs, _NOW)
    assert result.state == gg.REVIEWING
    assert "reviewing" in result.message


def test_an_open_review_run_holds_back_a_pass_too() -> None:
    runs = [_run(status="queued", started=None)]
    result = gg.verdict([_comment(_summary(_HEAD, 5))], _HEAD, runs, _NOW)
    assert result.state == gg.REVIEWING


def test_an_open_review_run_of_an_unreviewed_head_is_reviewing() -> None:
    runs = [_run(status="in_progress", started="2026-11-01T15:00:00Z")]
    result = gg.verdict([_comment(_summary(_OLD, 5))], _HEAD, runs, _NOW)
    assert result.state == gg.REVIEWING


def test_a_run_closed_before_the_summary_is_edited_waits() -> None:
    # Greptile closes its run a few seconds before it edits the summary.
    runs = [_run(started="2026-11-01T15:00:00Z", completed="2026-11-01T15:02:00Z")]
    moments_later = datetime(2026, 11, 1, 15, 2, 3, tzinfo=UTC)
    result = gg.verdict([_comment(_summary(_HEAD, 3))], _HEAD, runs, moments_later)
    assert result.state == gg.REVIEWING


def test_a_run_long_closed_without_a_score_is_absent() -> None:
    # Out of credits: the run closes and no summary of the head ever comes.
    runs = [_run(started="2026-11-01T15:00:00Z", completed="2026-11-01T15:00:05Z")]
    result = gg.verdict([_comment(_summary(_OLD, 3))], _HEAD, runs, _NOW)
    assert result.state == gg.ABSENT


def test_comments_are_read_across_pages() -> None:
    filler = [_comment("ordinary comment", login="someone")] * gg.PAGE_SIZE
    pages = {1: filler, 2: [_comment(_summary(_HEAD, 5))]}
    seen: list[str] = []

    def fetch(path: str) -> Any:
        seen.append(path)
        return pages[int(path.rsplit("page=", 1)[1])]

    comments = gg.pr_comments(fetch, "o/r", 7)
    assert len(comments) == gg.PAGE_SIZE + 1
    assert seen == [
        "/repos/o/r/issues/7/comments?per_page=100&page=1",
        "/repos/o/r/issues/7/comments?per_page=100&page=2",
    ]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return datetime(2026, 11, 1, 15, tzinfo=UTC) + timedelta(seconds=self.now)

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _wait(
    replies: list[Any],
    runs: list[Any] | None = None,
    wait_s: float = 600,
    grace_s: float = 300,
) -> tuple[gg.Verdict, int]:
    clock = _Clock()
    calls = 0

    def fetch(path: str) -> Any:
        nonlocal calls
        if "/check-runs?" in path:
            return {"check_runs": runs or []}
        reply = replies[min(calls, len(replies) - 1)]
        calls += 1
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = gg.wait_for_verdict(
        fetch,
        "o/r",
        7,
        _HEAD,
        wait_s=wait_s,
        grace_s=grace_s,
        poll_s=60,
        clock=clock,
        wall=clock.wall,
        sleep=clock.sleep,
    )
    return result, calls


def test_the_gate_waits_for_the_review_of_the_head_to_land() -> None:
    replies = [[], [_comment(_summary(_OLD, 5))], [_comment(_summary(_HEAD, 5))]]
    result, calls = _wait(replies)
    assert result.state == gg.PASS
    assert calls == 3


def test_a_late_low_score_within_the_grace_period_still_fails() -> None:
    replies = [[], [], [_comment(_summary(_HEAD, 3))]]
    assert _wait(replies)[0].state == gg.FAIL


def test_the_gate_passes_with_a_warning_when_greptile_never_reviews() -> None:
    # Greptile out of credits: no review of the head, so the gate does not apply.
    result, calls = _wait([[_comment(_summary(_OLD, 3))]])
    assert result.state == gg.SKIP
    assert "does not apply" in result.message
    assert calls == 6  # read at 0, 60, ... 300 s: the grace period, not the full wait


def test_a_review_that_never_posts_a_score_does_not_block() -> None:
    runs = [_run(status="in_progress", started="2026-11-01T15:00:00Z")]
    result, _ = _wait([[]], runs=runs)
    assert result.state == gg.SKIP
    assert "posted no score" in result.message


@pytest.mark.parametrize(("known", "state"), [(3, gg.FAIL), (5, gg.PASS)])
def test_a_stuck_re_review_leaves_the_known_score_of_the_head(
    known: int, state: str
) -> None:
    # A known 3/5 must not expire into a pass because a re-review never ends.
    runs = [_run(), _run(status="in_progress", started="2026-11-01T15:00:00Z")]
    result, _ = _wait([[_comment(_summary(_HEAD, known))]], runs=runs)
    assert result.state == state
    assert f"{known}/5" in result.message
    assert "stands" in result.message


def test_an_unreadable_reply_is_retried_not_passed() -> None:
    replies = [gg.GateError("boom"), {"message": "odd"}, [_comment(_summary(_HEAD, 5))]]
    assert _wait(replies)[0].state == gg.PASS


def test_github_unreadable_for_the_whole_wait_fails() -> None:
    # Not knowing whether Greptile scored is not Greptile being absent.
    result, _ = _wait([gg.GateError("boom")])
    assert result.state == gg.FAIL
    assert "Could not read GitHub" in result.message


def _routed(comments: list[Any], runs: list[Any]) -> Any:
    def fetch(path: str) -> Any:
        if "/check-runs?" in path:
            return {"total_count": len(runs), "check_runs": runs}
        return comments

    return fetch


def test_main_rejects_a_bad_head() -> None:
    assert gg.main(["greptile_gate.py", "7", "not-a-sha"], fetch=_routed([], [])) == 1


def test_main_exits_by_the_verdict() -> None:
    low = _routed([_comment(_summary(_HEAD, 3))], [_run()])
    assert gg.main(["greptile_gate.py", "7", _HEAD], fetch=low) == 1
    top = _routed([_comment(_summary(_HEAD, 5))], [_run()])
    assert gg.main(["greptile_gate.py", "7", _HEAD], fetch=top) == 0
    unscored = _routed([_comment(_summary(_HEAD, None))], [_run()])
    assert gg.main(["greptile_gate.py", "7", _HEAD], fetch=unscored) == 0


def test_main_passes_when_greptile_is_absent() -> None:
    clock = _Clock()
    argv = ["greptile_gate.py", "7", _HEAD]
    absent = _routed([], [])
    assert gg.main(argv, fetch=absent, clock=clock, sleep=clock.sleep) == 0


def test_only_greptiles_own_review_run_counts() -> None:
    # A workflow job named "Greptile Review" is not Greptile reviewing.
    runs = [_run(status="in_progress", slug="github-actions"), _run()]
    seen: list[str] = []

    def fetch(path: str) -> Any:
        seen.append(path)
        return {"check_runs": runs}

    assert gg.review_runs(fetch, "o/r", _HEAD) == [_run()]
    assert seen == [
        f"/repos/o/r/commits/{_HEAD}/check-runs?check_name=Greptile%20Review&per_page=100"
    ]


def test_the_gate_waits_out_a_same_head_re_review() -> None:
    clock = _Clock()
    replies = iter(
        [
            ([_run(status="in_progress", started="2026-11-01T15:00:00Z")], 3),
            ([_run(started="2026-11-01T15:00:00Z")], 5),
        ]
    )
    state: dict[str, Any] = {}

    def fetch(path: str) -> Any:
        if "/check-runs?" in path:
            state["runs"], state["score"] = next(replies)
            return {"check_runs": state["runs"]}
        stamp = "2026-11-01T14:00:00Z" if state["score"] == 3 else "2026-11-01T15:03Z"
        return [_comment(_summary(_HEAD, state["score"]), updated=stamp)]

    result = gg.wait_for_verdict(
        fetch, "o/r", 7, _HEAD, wait_s=300, poll_s=60, clock=clock, sleep=clock.sleep
    )
    assert result.state == gg.PASS
    assert clock.now == 60


def test_the_required_summary_job_fails_unless_the_gate_passes() -> None:
    # The ruleset requires only `All Checks Pass`, so the gate blocks a merge
    # only through that job.
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    gate = jobs["greptile-gate"]
    assert "scripts/greptile_gate.py" in gate["steps"][-1]["run"]
    summary = jobs["all-checks-pass"]
    assert "greptile-gate" in summary["needs"]
    assert "needs.greptile-gate.result" in summary["steps"][0]["run"]
