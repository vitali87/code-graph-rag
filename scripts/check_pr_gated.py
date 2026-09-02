"""Report whether a pull request is actually gated, not merely green.

`mergeStateStatus=CLEAN` plus a green check list is the combination that
normally reads as ready, and on this repository it can be fully satisfied
by a PR that nothing verified (issues #1581, #1582). Three independent
reasons, any one sufficient:

* The active ruleset's conditions are `ref_name.include = ["~DEFAULT_BRANCH"]`,
  so a PR based on a sibling branch is required to satisfy NOTHING. Its
  CLEAN status means "nothing is required", not "everything passed", and
  the two are indistinguishable from the checks list alone.
* A check that never ran is not failing. A rebase arriving mid-run cancels
  it, so a rapidly-moving branch can leave its head with only the fast
  unfiltered jobs and report zero failures out of a set containing no tests.
* CodeRabbit's auto-review is skipped on non-default bases, and the skip is
  a comment with a non-empty body, so a comment COUNT cannot see it.

This script asks the positive question in each case -- is a run present at
this head, is each required context present, did a review actually happen,
is this base covered by a rule -- because every corresponding negative is
satisfied by absence.

It is ADVISORY. It reads GitHub through the ambient `gh` auth and writes
nothing. Closing the gap for real means widening the ruleset's
`ref_name.include`, which is repository settings and the owner's decision.

Usage:  uv run python scripts/check_pr_gated.py <pr-number>
Exit 0 when every check passes, 1 otherwise, printing EVERY reason found.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

REPO = "vitali87/code-graph-rag"

# The one context the active ruleset requires on the default branch. It
# aggregates the jobs below and asserts each result == "success", so a
# skipped or cancelled job fails it rather than passing silently.
REQUIRED_CONTEXT = "All Checks Pass"

AGGREGATED_JOBS = (
    "Lint & Format",
    "Type Check",
    "Unit Tests",
    "Integration Tests",
    "Binary Smoke Test",
)

# A review artifact must POSITIVELY carry a verdict. The alternative --
# rejecting a list of known skip notices -- fails in the wrong direction:
# the wording is not a closed set (three variants are already in the wild,
# and #1581 was filed knowing two), so a blocklist admits every future
# variant by default.
REVIEW_VERDICT_MARKERS = (
    "actionable comments posted",
    "no actionable comments were generated",
    "last reviewed commit",
    "confidence score",
)


def _gh(*args: str) -> str:
    """`gh` stdout, or "" when the call fails.

    Failures are returned as empty rather than raised so one unavailable
    endpoint reports as its own named reason instead of aborting the run
    and leaving the other checks unreported.
    """
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def context_name(entry: dict[str, object]) -> str:
    """The display name of a `statusCheckRollup` entry, whichever shape it is.

    A `CheckRun` carries `name`; a `StatusContext` carries `context` and has
    no `name` key at all. The jq form `select(.name | test(...))` raises on
    the second, and that error reads as "no matching contexts" if stderr
    scrolls past -- an instrument failure that looks like a measurement.
    """
    for key in ("name", "context"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def is_concluded(entry: dict[str, object]) -> bool:
    """Whether a check has finished.

    A queued check reports `conclusion: ""` -- an empty STRING, not null --
    so `.conclusion // "pending"` in jq and `entry.get("conclusion", ...)`
    in Python both fail to default, and the entry reads as concluded.
    """
    conclusion = entry.get("conclusion")
    return isinstance(conclusion, str) and conclusion != ""


def unit_test_contexts(rollup: list[dict[str, object]]) -> list[str]:
    """Names of the unit-test contexts only.

    `startswith` rather than a loose `test` match: `Integration Tests` and
    `Binary Smoke Test` are real checks and not unit coverage, and a loose
    pattern reports them as such.
    """
    return [
        name
        for entry in rollup
        if (name := context_name(entry)).startswith("Unit Tests")
    ]


def missing_aggregated_jobs(rollup: list[dict[str, object]]) -> list[str]:
    """Jobs `All Checks Pass` aggregates that have no context at this head.

    Matched by PREFIX because the matrix jobs carry their platform in the
    name (`Unit Tests (ubuntu-latest, py3.12)`), so an exact comparison
    finds none of them and would report every job missing.

    Checking these as well as the aggregate is the difference between
    trusting the gate and verifying it: `All Checks Pass` is a job like any
    other, and if it never ran then its own absence is what to catch.
    """
    names = [context_name(entry) for entry in rollup]
    return [job for job in AGGREGATED_JOBS if not any(n.startswith(job) for n in names)]


def required_contexts_present(
    rollup: list[dict[str, object]], required: list[str]
) -> list[str]:
    """Required names that are ABSENT from `rollup`.

    Presence, not success: a context that never ran is not failing, so
    asking "did anything fail" returns clean for a PR that ran nothing.
    """
    present = {context_name(entry) for entry in rollup}
    return [name for name in required if name not in present]


def is_real_review(body: str) -> bool:
    """Whether a comment body is a review rather than a notice about one.

    Positive detection, so an unrecognised notice fails closed. A review
    that found nothing still counts -- it ran -- which is why emptiness of
    findings cannot be the test either.
    """
    lowered = body.strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in REVIEW_VERDICT_MARKERS)


def _json_dict(text: str) -> dict[str, Any]:
    """Parsed object, or `{}` when the call failed or returned something else.

    Split from the list form rather than returning a bare `object`, so the
    call sites keep their types instead of every `.get` needing a cast.
    """
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(text: str) -> list[Any]:
    """Parsed array, or `[]` when the call failed or returned something else."""
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def check(pr: str) -> list[str]:
    """Every reason `pr` is not verifiably gated, in order. Empty means gated."""
    reasons: list[str] = []

    view = _json_dict(
        _gh(
            "pr",
            "view",
            pr,
            "--repo",
            REPO,
            "--json",
            "headRefOid,baseRefName,statusCheckRollup,comments,reviews",
        )
    )
    if not view:
        return [f"could not read PR #{pr} (is `gh` authenticated?)"]

    head = str(view.get("headRefOid", ""))
    base = str(view.get("baseRefName", ""))
    rollup = [e for e in view.get("statusCheckRollup", []) if isinstance(e, dict)]

    rules = _json_list(_gh("api", f"repos/{REPO}/rules/branches/{base}"))
    rule_types = {r.get("type") for r in rules if isinstance(r, dict)}
    if "required_status_checks" not in rule_types:
        reasons.append(
            f"base '{base}' is covered by no ruleset requiring status checks, "
            "so nothing is enforced on this PR regardless of its check list"
        )

    runs = _json_list(
        _gh(
            "run",
            "list",
            "--repo",
            REPO,
            "--workflow",
            "CI",
            "--limit",
            "40",
            "--json",
            "headSha,databaseId",
        )
    )
    at_head = [
        r for r in runs if isinstance(r, dict) and str(r.get("headSha", "")) == head
    ]
    if not at_head:
        reasons.append(f"no CI run exists at the head SHA {head[:8]}")
    else:
        owners: set[str] = set()
        for run in at_head:
            detail = _json_dict(
                _gh("api", f"repos/{REPO}/actions/runs/{run.get('databaseId')}")
            )
            owners.update(
                str(p.get("number"))
                for p in detail.get("pull_requests", [])
                if isinstance(p, dict)
            )
        if owners and pr not in owners:
            reasons.append(
                f"the CI run at {head[:8]} belongs to PR(s) {sorted(owners)}, not #{pr}; "
                "branches sharing a head SHA after a rebase report each other's runs"
            )

    missing = required_contexts_present(rollup, [REQUIRED_CONTEXT])
    if missing:
        reasons.append(f"required context absent at the head: {missing}")
    else:
        for entry in rollup:
            if context_name(entry) != REQUIRED_CONTEXT:
                continue
            if not is_concluded(entry):
                reasons.append(f"'{REQUIRED_CONTEXT}' has not concluded")
            elif str(entry.get("conclusion", "")).upper() != "SUCCESS":
                reasons.append(
                    f"'{REQUIRED_CONTEXT}' concluded {entry.get('conclusion')}"
                )

    absent_jobs = missing_aggregated_jobs(rollup)
    if absent_jobs:
        reasons.append(
            f"jobs aggregated by '{REQUIRED_CONTEXT}' have no context at the head: "
            f"{absent_jobs}; a green aggregate over a set containing none of these "
            "reports on nothing"
        )

    bodies = [
        str(c.get("body", "")) for c in view.get("comments", []) if isinstance(c, dict)
    ] + [str(r.get("body", "")) for r in view.get("reviews", []) if isinstance(r, dict)]
    if not any(is_real_review(b) for b in bodies):
        reasons.append(
            f"no review artifact carries a verdict ({len(bodies)} comment(s)/review(s) "
            "present, none of which is a review)"
        )

    threads = _json_dict(
        _gh(
            "api",
            "graphql",
            "-f",
            "query="
            '{repository(owner:"vitali87",name:"code-graph-rag")'
            f"{{pullRequest(number:{pr})"
            "{reviewThreads(first:100){nodes{isResolved}}}}}",
        )
    )
    try:
        nodes = threads["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        unresolved = sum(1 for n in nodes if n.get("isResolved") is False)
    except (KeyError, TypeError, AttributeError):
        unresolved = 0
        reasons.append("could not read review threads, so resolution is unverified")
    if unresolved:
        reasons.append(f"{unresolved} unresolved review thread(s)")

    return reasons


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        sys.stderr.write(f"usage: {argv[0]} <pr-number>\n")
        return 2
    pr = argv[1]
    reasons = check(pr)
    if not reasons:
        sys.stdout.write(f"PR #{pr}: gated (every check present and satisfied)\n")
        return 0
    sys.stdout.write(f"PR #{pr}: NOT verifiably gated -- {len(reasons)} reason(s)\n")
    for reason in reasons:
        sys.stdout.write(f"  - {reason}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
