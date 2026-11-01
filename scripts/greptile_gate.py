#!/usr/bin/env python3
"""Fail a pull request whose head commit Greptile did not score 5/5.

Greptile's review ends in a confidence score, and nothing read it: a PR
scored 3/5 with an open security finding showed every check green. This
gate waits for Greptile's review of the exact head commit and passes only
on 5/5. A score of an older commit is not a score of the code about to
merge, so it neither passes nor fails the gate: the gate keeps waiting for
the review of the head, and fails if none arrives in time.

Only a comment written by Greptile's own app account counts. Anyone can
type the score marker into a comment of their own.

Usage:  python3 scripts/greptile_gate.py <pr-number> <head-sha>
Reads GH_TOKEN (or GITHUB_TOKEN) and GITHUB_REPOSITORY. Exit 0 on a 5/5
review of the head, 1 otherwise. Standard library only, so CI runs it
without syncing the project environment.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

API = "https://api.github.com"
DEFAULT_REPO = "vitali87/code-graph-rag"
TIMEOUT_S = 30
ATTEMPTS = 3
RETRY_WAIT_S = 10
PAGE_SIZE = 100
# Greptile posts its review a few minutes after a push; a large PR takes
# longer. The job's own timeout in ci.yml sits above this.
WAIT_S = 45 * 60
POLL_S = 60

REQUIRED_SCORE = 5
MAX_SCORE = 5
# The `[bot]` suffix cannot appear in a user's login, so this one name is
# Greptile's app and nobody else.
GREPTILE_LOGIN = "greptile-apps[bot]"
SUMMARY_MARKER = "<!-- greptile_summary -->"
# Greptile opens this check run when a review starts and closes it just
# before it edits the summary, so it tells a review in flight from a
# finished one. The app's slug is its bot login without `[bot]`.
REVIEW_CHECK = "Greptile Review"
GREPTILE_APP = "greptile-apps"
COMPLETED = "completed"
SCORE_PATTERNS = (
    re.compile(r"<!--\s*greptile_confidence_score:\s*(\d+)\s*-->"),
    re.compile(r"Confidence Score:\s*(\d+)\s*/\s*5", re.I),
)
REVIEWED_COMMIT = re.compile(
    r"Last reviewed commit:.{0,400}?/commit/([0-9a-f]{40})", re.I | re.S
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")

PASS = "pass"
FAIL = "fail"
WAIT = "wait"

USAGE = "usage: greptile_gate.py <pr-number> <head-sha>"
PASSED = "Greptile scored {sha} {score}/{max}: {url}"
LOW_SCORE = (
    "Greptile scored {sha} {score}/{max}; {required}/{max} is required. "
    "Fix its findings and push, and Greptile reviews the new head. If a "
    "finding is wrong, answer it in its thread, comment `@greptile review` to ask "
    "for a re-review, and re-run this job once the Greptile Review check has "
    "started. Review: {url}"
)
NO_SCORE = (
    "Greptile reviewed {sha} but its summary carries no confidence score, so "
    "the gate cannot read one. Review: {url}"
)
NO_REVIEW = "Greptile has not reviewed this pull request yet."
REVIEWING = "Greptile is reviewing {sha}. Waiting for its score."
STALE = (
    "Greptile's latest review is of {reviewed}, not the head {sha}. Waiting "
    "for its review of the head."
)
GAVE_UP = (
    "No Greptile review of {sha} within {minutes} min. {state} If Greptile "
    "did not start, comment `@greptile review` on the pull request to ask for a "
    "review, then re-run this job."
)

Fetch = Callable[[str], Any]


class GateError(Exception):
    pass


@dataclass(frozen=True)
class Verdict:
    state: str
    message: str


def is_greptile_summary(comment: dict[str, Any]) -> bool:
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    body = comment.get("body")
    return login == GREPTILE_LOGIN and isinstance(body, str) and SUMMARY_MARKER in body


def reviewed_commit(body: str) -> str:
    found = REVIEWED_COMMIT.search(body)
    return found.group(1).lower() if found else ""


def score(body: str) -> int | None:
    for pattern in SCORE_PATTERNS:
        found = pattern.search(body)
        if found:
            return int(found.group(1))
    return None


def _updated(comment: dict[str, Any]) -> str:
    stamp = comment.get("updated_at") or comment.get("created_at")
    return stamp if isinstance(stamp, str) else ""


def _moment(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def review_in_flight(runs: Sequence[Any], summary: dict[str, Any] | None) -> bool:
    """Whether Greptile is still producing the score of the head.

    A re-review of the same commit (`@greptile review` without a push) leaves
    the old score of that very commit in place until it finishes, and the
    run closes a few seconds before the summary is edited. So a run still
    open, or one that started after the summary last changed, means the
    summary is about to change and its score is not the answer yet.
    """
    edited = _moment(_updated(summary)) if summary else None
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("status") != COMPLETED:
            return True
        started = _moment(run.get("started_at"))
        if summary and (started is None or edited is None or started > edited):
            return True
    return False


def verdict(comments: list[Any], head: str, runs: Sequence[Any] = ()) -> Verdict:
    """What Greptile's comments say about `head`.

    Greptile edits one summary comment in place as it re-reviews, so the
    newest summary naming the head is the score of the head, once no review
    of the head is in flight (`runs` are Greptile's review check runs on the
    head). A summary that names another commit says nothing about this one.
    """
    head = head.lower()
    summaries = sorted(
        (c for c in comments if isinstance(c, dict) and is_greptile_summary(c)),
        key=_updated,
    )
    if not summaries:
        return Verdict(WAIT, NO_REVIEW)
    of_head = [c for c in summaries if reviewed_commit(c["body"]) == head]
    if not of_head:
        reviewed = reviewed_commit(summaries[-1]["body"]) or "an unnamed commit"
        return Verdict(WAIT, STALE.format(reviewed=reviewed[:8], sha=head[:8]))
    latest = of_head[-1]
    if review_in_flight(runs, latest):
        return Verdict(WAIT, REVIEWING.format(sha=head[:8]))
    url = latest.get("html_url", "")
    found = score(latest["body"])
    if found is None:
        return Verdict(FAIL, NO_SCORE.format(sha=head[:8], url=url))
    fields = {"sha": head[:8], "score": found, "max": MAX_SCORE, "url": url}
    if found == REQUIRED_SCORE:
        return Verdict(PASS, PASSED.format(**fields))
    return Verdict(FAIL, LOW_SCORE.format(required=REQUIRED_SCORE, **fields))


def pr_comments(fetch: Fetch, repo: str, pr: int) -> list[Any]:
    comments: list[Any] = []
    page = 1
    while True:
        batch = fetch(
            f"/repos/{repo}/issues/{pr}/comments?per_page={PAGE_SIZE}&page={page}"
        )
        if not isinstance(batch, list):
            raise GateError(f"unreadable comments page {page}")
        comments.extend(batch)
        if len(batch) < PAGE_SIZE:
            return comments
        page += 1


def review_runs(fetch: Fetch, repo: str, head: str) -> list[Any]:
    reply = fetch(
        f"/repos/{repo}/commits/{head}/check-runs"
        f"?check_name={quote(REVIEW_CHECK)}&per_page={PAGE_SIZE}"
    )
    runs = reply.get("check_runs") if isinstance(reply, dict) else None
    if not isinstance(runs, list):
        raise GateError("unreadable check runs")
    # Any workflow can name a job "Greptile Review"; only Greptile's app counts.
    return [
        run
        for run in runs
        if isinstance(run, dict)
        and isinstance(run.get("app"), dict)
        and run["app"].get("slug") == GREPTILE_APP
    ]


def wait_for_verdict(
    fetch: Fetch,
    repo: str,
    pr: int,
    head: str,
    *,
    wait_s: float = WAIT_S,
    poll_s: float = POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Verdict:
    deadline = clock() + wait_s
    while True:
        try:
            # Runs first: a review that finishes between the two reads then
            # shows as a run newer than the summary, never as a stale score.
            runs = review_runs(fetch, repo, head)
            current = verdict(pr_comments(fetch, repo, pr), head, runs)
        except (GateError, urllib.error.URLError, TimeoutError) as error:
            # A failed read is not an answer either way; ask again.
            current = Verdict(WAIT, f"Could not read the comments ({error}).")
        if current.state != WAIT:
            return current
        if clock() >= deadline:
            return Verdict(
                FAIL,
                GAVE_UP.format(
                    sha=head[:8], minutes=round(wait_s / 60), state=current.message
                ),
            )
        _report(current.message, error=False)
        sleep(poll_s)


def github_fetch(token: str | None) -> Fetch:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(path: str) -> Any:
        request = urllib.request.Request(API + path, headers=headers)  # noqa: S310 - fixed https://api.github.com URL
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed https://api.github.com URL
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == ATTEMPTS:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == ATTEMPTS:
                    raise
            time.sleep(RETRY_WAIT_S)
        raise GateError("no reply")

    return fetch


def _report(message: str, *, error: bool) -> None:
    if error and os.environ.get("GITHUB_ACTIONS") == "true":
        message = f"::error::{message}"
    print(message, file=sys.stderr if error else sys.stdout, flush=True)  # noqa: T201


def main(argv: list[str], fetch: Fetch | None = None, **wait: Any) -> int:
    if (
        len(argv) != 3
        or not argv[1].isdigit()
        or not SHA_PATTERN.fullmatch(argv[2].lower())
    ):
        _report(USAGE, error=True)
        return 1
    pr, head = int(argv[1]), argv[2].lower()
    repo = os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    if fetch is None:
        fetch = github_fetch(
            os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        )
    result = wait_for_verdict(fetch, repo, pr, head, **wait)
    _report(result.message, error=result.state != PASS)
    return 0 if result.state == PASS else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
