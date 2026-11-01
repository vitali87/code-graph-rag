# Greptile scored a pull request 3/5 with an open security finding and every
# check stayed green, because nothing read the score. The gate now requires
# Greptile's review of the exact head commit to be 5/5.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import greptile_gate as gg

_REPO = Path(__file__).resolve().parents[2]
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_HEAD = "5218d78ab933e8d1deb58e018f96c9b9390831a7"
_OLD = "86db96085b4a0a1b6b9409d9399de056f36d6b79"


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


def test_a_review_of_the_head_without_a_score_fails_closed() -> None:
    result = gg.verdict([_comment(_summary(_HEAD, None))], _HEAD)
    assert result.state == gg.FAIL
    assert "no confidence score" in result.message


def test_a_5_of_5_review_of_an_older_commit_does_not_pass() -> None:
    # A score of the code before the last push says nothing about the head.
    result = gg.verdict([_comment(_summary(_OLD, 5))], _HEAD)
    assert result.state == gg.WAIT
    assert _OLD[:8] in result.message


def test_no_greptile_review_waits() -> None:
    assert gg.verdict([], _HEAD).state == gg.WAIT


def test_the_trex_progress_comment_is_not_a_review() -> None:
    trex = "<!-- greptile_trex_summary -->\n\nTREX is testing this pull request"
    assert gg.verdict([_comment(trex)], _HEAD).state == gg.WAIT


@pytest.mark.parametrize("login", ["vitali87", "greptile-apps", "greptileai"])
def test_a_score_written_by_anyone_but_greptiles_app_is_ignored(login: str) -> None:
    # The marker is plain text: the author could post a 5/5 of their own.
    forged = _comment(_summary(_HEAD, 5), login=login)
    assert gg.verdict([forged], _HEAD).state == gg.WAIT


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

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _wait(replies: list[Any], wait_s: float = 300) -> tuple[gg.Verdict, int]:
    clock = _Clock()
    calls = 0

    def fetch(path: str) -> Any:
        nonlocal calls
        reply = replies[min(calls, len(replies) - 1)]
        calls += 1
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = gg.wait_for_verdict(
        fetch, "o/r", 7, _HEAD, wait_s=wait_s, poll_s=60, clock=clock, sleep=clock.sleep
    )
    return result, calls


def test_the_gate_waits_for_the_review_of_the_head_to_land() -> None:
    replies = [[], [_comment(_summary(_OLD, 5))], [_comment(_summary(_HEAD, 5))]]
    result, calls = _wait(replies)
    assert result.state == gg.PASS
    assert calls == 3


def test_the_gate_fails_when_no_review_of_the_head_arrives() -> None:
    result, _ = _wait([[_comment(_summary(_OLD, 5))]])
    assert result.state == gg.FAIL
    assert "No Greptile review" in result.message


def test_an_unreadable_reply_is_retried_not_passed() -> None:
    replies = [gg.GateError("boom"), {"message": "odd"}, [_comment(_summary(_HEAD, 5))]]
    assert _wait(replies)[0].state == gg.PASS


def test_main_rejects_a_bad_head() -> None:
    assert gg.main(["greptile_gate.py", "7", "not-a-sha"], fetch=lambda _: []) == 1


def test_main_exits_by_the_verdict() -> None:
    def fetch(_: str) -> Any:
        return [_comment(_summary(_HEAD, 3))]

    assert gg.main(["greptile_gate.py", "7", _HEAD], fetch=fetch) == 1

    def fetch5(_: str) -> Any:
        return [_comment(_summary(_HEAD, 5))]

    assert gg.main(["greptile_gate.py", "7", _HEAD], fetch=fetch5) == 0


def test_the_required_summary_job_fails_unless_the_gate_passes() -> None:
    # The ruleset requires only `All Checks Pass`, so the gate blocks a merge
    # only through that job.
    jobs = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    gate = jobs["greptile-gate"]
    assert "scripts/greptile_gate.py" in gate["steps"][-1]["run"]
    summary = jobs["all-checks-pass"]
    assert "greptile-gate" in summary["needs"]
    assert "needs.greptile-gate.result" in summary["steps"][0]["run"]
