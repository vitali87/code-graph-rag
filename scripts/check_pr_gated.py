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

Intended for an OPEN pull request, immediately before merging. Run against
an already-merged one it will usually report the CI run as unresolvable:
GitHub leaves `pull_requests: []` on runs belonging to closed PRs, and this
script treats an unanswerable ownership question as unverified rather than
clean. That is the deliberate direction -- an empty answer is not a pass --
but it makes the tool noisy after the fact, which is not what it is for.

Usage:  uv run python scripts/check_pr_gated.py <pr-number>
Exit 0 when every check passes, 1 otherwise, printing EVERY reason found.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

REPO = "vitali87/code-graph-rag"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"

# A CI run in one of these states has not finished, so its check contexts are
# still coming. `queued` is the one that matters: it contributes no rollup
# entry at all, which is indistinguishable from "never ran" without this
# (#1848).
CI_RUN_UNFINISHED_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)

# The events whose runs create a PR's check contexts. `workflow_dispatch` and
# `push` runs can sit at the same head SHA and report under their own event,
# so they are evidence about themselves, not about this PR (#1848). Defaulted
# to `pull_request` when absent, so a response without the field behaves as
# before rather than silently dropping every run.
CI_RUN_PR_EVENTS = frozenset({"pull_request", "pull_request_target"})

# The one context the active ruleset requires on the default branch. It
# aggregates the jobs below and asserts each result == "success", so a
# skipped or cancelled job fails it rather than passing silently.
REQUIRED_CONTEXT = "All Checks Pass"

# Every job `all-checks-pass` declares in `needs:`, by DISPLAY name -- the
# rollup carries names, not job ids. Five were listed and four were missing,
# so a head where only a missing one had reported looked like "no dependency
# reported at all" (#1848). `Unit Tests (base install)` needs no entry of its
# own: the matrix rule matches it under `Unit Tests`.
#
# This list must stay in step with ci.yml's `needs:` block. It cannot be
# derived at runtime -- the rollup gives display names and `needs:` gives job
# ids, with no mapping available without parsing the workflow -- so the
# coupling is real and worth stating rather than hiding.
AGGREGATED_JOBS = (
    "Lint & Format",
    "Type Check",
    "Unit Tests",
    "Integration Tests",
    "Binary Smoke Test",
    "Wheel Smoke (unlocked resolution)",
    "Go Frontend",
    "Sonar Zero Issues Gate",
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

# A verdict is only evidence if the account that wrote it is the one whose
# review the gate is asking about. Without this the check is spoofable by
# the author it is meant to constrain: writing "confidence score" in an
# ordinary comment satisfies a marker-only test (CWE-345, Greptile on
# PR #1625). Bot logins carry the `[bot]` suffix on some payloads and not
# others, so both spellings are accepted.
TRUSTED_REVIEWERS = frozenset(
    {
        "coderabbitai",
        "coderabbitai[bot]",
        "greptile-apps",
        "greptile-apps[bot]",
        "greptileai",
        "gemini-code-assist",
        "gemini-code-assist[bot]",
    }
)


# A reviewer that could not RUN anything still produces a full-looking
# review: the same verdict line, the same confidence score, findings that
# read identically to executed ones. The blocked-validation note lands in a
# collapsed log section that a reader skims past and a gate never opens
# (#1824). These substrings are how that note has actually been worded on
# this repo, in the reviewers' own text:
#
#   "the test suite remains blocked in this environment"
#   "failed during import with ModuleNotFoundError: No module named ..."
#
# A marker must be SELF-ANCHORING: it can only describe the reviewer's own
# situation, never the reviewed code's. That rules out any phrase naming a
# failure mode an application can also have. Three rounds of narrowing, each
# after a false positive was demonstrated:
#
#   "could not start" / "modulenotfounderror"  -- "the server could not
#       start; it raises KeyError" is a finding about a BUG.
#   "failed during import with modulenotfounderror" -- still a bug when the
#       reviewed code is what failed to import.
#   "dependency installation is blocked"       -- an installer defect.
#   "validation blocked"                       -- a validation FEATURE
#       rejecting input is normal application behaviour.
#
# What survives names the environment a review runs IN, which reviewed code
# has no occasion to discuss. Flagging an executed review as unexecuted
# discredits work that was actually done, so a false positive costs more
# than a miss.
#
# Unlike REVIEW_VERDICT_MARKERS this IS a blocklist, and it fails in the
# permissive direction on purpose. A missed variant reports the review as
# ordinary -- exactly today's behaviour -- while a false positive would
# nag about a review that ran fine. Non-execution is also legitimate and
# common: a YAML-only PR has no Python surface to exercise, so this can
# never be a merge blocker. It is surfaced as a caveat, not a reason.
BLOCKED_VALIDATION_MARKERS = (
    "blocked in this environment",
    "could not be behaviorally disproved",
    "without a runnable import/test environment",
)


def validation_was_blocked(body: str) -> bool:
    """Whether a review says its own checks could not execute.

    Positive detection on the reviewer's own wording. Absence means "no
    such note was recognised", NOT "the review executed" -- the wording is
    not a closed set, so this understates rather than overstates. See
    BLOCKED_VALIDATION_MARKERS for why that direction is the safe one.
    """
    lowered = body.strip().lower()
    return any(marker in lowered for marker in BLOCKED_VALIDATION_MARKERS)


def review_execution_caveats(real_reviews: list[tuple[str, str]]) -> list[str]:
    """Caveats about reviews that say their own checks could not execute.

    Separate from `check` so the WIRING is testable, not only the
    detector. A test that exercises `validation_was_blocked` alone stays
    green when the caveat is never consulted, which is coverage that
    cannot fail for its stated reason.
    """
    if not real_reviews:
        return []
    blocked_by = sorted(
        {author for body, author in real_reviews if validation_was_blocked(body)}
    )
    if not blocked_by:
        return []
    if all(validation_was_blocked(body) for body, _ in real_reviews):
        return [
            f"every review artifact present says its own checks could not "
            f"execute ({', '.join(blocked_by)}); its findings may be right, "
            "but they rest on reasoning the reviewer could not confirm -- "
            "verify them yourself rather than reading the score as checked"
        ]
    return [
        f"a review by {', '.join(blocked_by)} says its own checks could "
        "not execute; treat its findings as reasoning-only"
    ]


def _gh_stdout_or_empty(*args: str) -> str:
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


def aggregated_job_for(name: str) -> str | None:
    """The `AGGREGATED_JOBS` entry `name` belongs to, or None.

    A bare prefix match is too loose. The matrix jobs carry their platform in
    a PARENTHESISED suffix (`Unit Tests (ubuntu-latest, py3.12)`), so an exact
    comparison matches none of them -- but `startswith` alone also claims
    `Unit Tests Coverage` and `Unit Testsimposter`, and an unrelated pending
    check misread as a dependency flips the verdict from "investigate" to
    "wait", which is the defect this function was added to fix (#1827).

    So: the exact name, or the name followed by ` (`. Nothing else.
    """
    for job in AGGREGATED_JOBS:
        if name == job or name.startswith(f"{job} ("):
            return job
    return None


def absent_context_reason(
    context: str,
    rollup: list[dict[str, object]],
    ci_runs: list[dict[str, Any]] | None = None,
) -> str:
    """Why `context` is missing: still running, not yet started, or absent.

    "Absent" collapses states that need opposite responses. `All Checks Pass`
    is an aggregate that reports only once its dependencies finish, so it is
    legitimately missing for a whole run -- yet the same sentence covered the
    #1582 case, where every job concluded and the aggregate never appeared.
    One says wait, the other says investigate, and the reassuring reading is
    the one a reader defaults to (#1827).

    EVERY FACT IS GATHERED BEFORE ANY ARM RUNS, and the arms are ordered
    most-specific first. Three separate review findings on this function were
    all the same defect -- a new early-return arm silently stealing cases from
    the arms after it -- because the conditions OVERLAP: a running dependency
    and an unfinished run are both true at once, and whichever was tested
    first won regardless of which was more informative. Computing the facts
    up front makes the overlap visible instead of implicit in the order
    (#1848).

    The three facts, and why each is ordered where it is:

    * `pending` -- a dependency of the aggregate is unfinished. The MOST
      specific, because it names the job the reader is waiting for.
    * `unstarted` -- a `pull_request` CI run exists but has produced no
      contexts. Less specific: it says the run is coming without saying what.
      Only a pull-request run counts; a `workflow_dispatch` or `push` run at
      the same SHA reports under its own event and creates none of this PR's
      contexts, so it is evidence about itself.
    * neither -- then either no dependency ever reported, or all of them
      concluded and the aggregate genuinely never arrived (#1582).
    """
    # Only the jobs the aggregate WAITS ON can explain its absence. Any
    # unfinished entry used to count, so a single unrelated pending check --
    # CodeRabbit is pending on nearly every PR here -- flipped the verdict
    # from "investigate" to "wait" while every dependency had concluded.
    pending = [
        entry
        for entry in rollup
        if not entry_finished(entry) and aggregated_job_for(context_name(entry))
    ]
    unstarted = [
        run
        for run in ci_runs or ()
        if str(run.get("status", "")) in CI_RUN_UNFINISHED_STATUSES
        and str(run.get("event", "pull_request")) in CI_RUN_PR_EVENTS
    ]
    any_dependency_reported = any(
        aggregated_job_for(context_name(entry)) for entry in rollup
    )

    # MOST SPECIFIC: a named dependency is still running.
    if pending:
        names = sorted(name for name in map(context_name, pending) if name)
        shown = ", ".join(names[:3]) + ("..." if len(names) > 3 else "")
        named = f" ({shown})" if names else ""
        return (
            f"'{context}' has not reported YET: {len(pending)} check(s) at the "
            f"head are still running{named}. This is CI in flight, not a "
            "missing run -- re-check rather than investigate"
        )
    # The run exists but has created no contexts yet. Checked after `pending`
    # so a running dependency is named rather than described as "no entries".
    if unstarted:
        statuses = sorted({str(run.get("status", "")) for run in unstarted})
        return (
            f"'{context}' has not reported YET: the CI run at the head is "
            f"{', '.join(statuses)} and has produced no check entries so far. "
            "This is CI not yet started, not a missing run -- re-check rather "
            "than investigate, and do NOT push an empty commit (it moves the "
            "head and discards any review anchored to it)"
        )
    if not rollup:
        return f"no check reported at the head at all, so '{context}' cannot appear"
    # Without this guard the sentence below claims every dependency concluded
    # when none was ever seen -- a claim about an empty set.
    if not any_dependency_reported:
        return (
            f"'{context}' is absent and NO check it aggregates reported at the "
            "head at all, so whether it is coming cannot be told from the "
            "rollup -- check whether CI ran for this head"
        )
    # "every check" would overclaim: `pending` counts only the entries this
    # aggregate DEPENDS on, so an unrelated pending check is deliberately
    # excluded and may still be running. Say what was actually examined.
    return (
        f"'{context}' is absent although every check it aggregates has "
        "concluded, so it is not going to appear"
    )


def is_concluded(entry: dict[str, object]) -> bool:
    """Whether a check has finished.

    A queued check reports `conclusion: ""` -- an empty STRING, not null --
    so `.conclusion // "pending"` in jq and `entry.get("conclusion", ...)`
    in Python both fail to default, and the entry reads as concluded.
    """
    conclusion = entry.get("conclusion")
    return isinstance(conclusion, str) and conclusion != ""


def entry_finished(entry: dict[str, object]) -> bool:
    """Whether a rollup entry has finished, whichever shape it is.

    `is_concluded` reads `conclusion`, which a `StatusContext` does not
    have -- it carries `state`. Judged by that predicate every third-party
    status is unfinished forever, so a rollup containing one can never
    reach the all-concluded branch and #1582 reports as "still running":
    the reassuring reading, in the one case that needs investigating.

    The older call site is guarded by a name test that only ever matches a
    `CheckRun`, so the gap was latent until `absent_context_reason` began
    judging EVERY entry (Greptile-local, PR for #1827).
    """
    if "conclusion" in entry or entry.get("__typename") == "CheckRun":
        return is_concluded(entry)
    state = entry.get("state")
    return isinstance(state, str) and state not in ("", "PENDING", "EXPECTED")


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


# A conclusion that is not a failure. SKIPPED and NEUTRAL are how a
# conditional job reports "did not apply", and branch protection treats both
# as satisfied, so counting them would fire the caveat below on almost every
# PR and teach the reader to scroll past it.
NON_FAILING_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})

# How many failing unrequired contexts to name before summarising the rest. An
# Actions outage reds many at once (#1941 was filed during one), and a caveat
# that prints thirty names is one nobody reads.
UNREQUIRED_CAVEAT_LIMIT = 8


def failing_unrequired_contexts(rollup: list[dict[str, object]]) -> list[str]:
    """Finished contexts that did not succeed and that THIS gate does not require.

    The gate answers "is `REQUIRED_CONTEXT` present and satisfied", so a red
    check outside it is correctly not a reason. It was not outside the
    READER's question: the verdict read as "nothing is failing", and a red
    check nobody was told about is the direction that produces a bad merge
    (#1941).

    "Unrequired" here means "not required BY THIS GATE", which is weaker than
    "not required by the ruleset". `check` reads the branch rule's `type` only,
    to answer whether any status-check rule covers the base; it never reads
    that rule's required-context list. So a context the ruleset does require
    would land in this set, and the caveat says so rather than calling it
    unrequired.

    `REQUIRED_CONTEXT` is excluded because a red one is already a reason, and
    naming it twice reads as two problems. The jobs it AGGREGATES are not
    excluded: `All Checks Pass` reports one verdict over all of them, and
    naming the job that actually failed is the point.
    """
    names = {
        name
        for entry in rollup
        if (name := context_name(entry))
        and name != REQUIRED_CONTEXT
        and entry_finished(entry)
        and (
            outcome := str(entry.get("conclusion") or entry.get("state") or "").upper()
        )
        and outcome not in NON_FAILING_CONCLUSIONS
    }
    return sorted(names)


def unrequired_failure_caveat(rollup: list[dict[str, object]]) -> list[str]:
    """The caveat for `failing_unrequired_contexts`, or nothing."""
    failing = failing_unrequired_contexts(rollup)
    if not failing:
        return []
    shown = failing[:UNREQUIRED_CAVEAT_LIMIT]
    rest = len(failing) - len(shown)
    return [
        f"{len(failing)} check(s) this gate does not require are failing at the "
        f"head: {', '.join(shown)}" + (f" (+{rest} more)" if rest else "") + ". "
        f"The gate requires only '{REQUIRED_CONTEXT}' and does not read the "
        "ruleset's required-context list, so it cannot say whether these are "
        "required. They do not block it, and they are not evidence the change "
        "is sound"
    ]


def required_contexts_present(
    rollup: list[dict[str, object]], required: list[str]
) -> list[str]:
    """Required names that are ABSENT from `rollup`.

    Presence, not success: a context that never ran is not failing, so
    asking "did anything fail" returns clean for a PR that ran nothing.
    """
    present = {context_name(entry) for entry in rollup}
    return [name for name in required if name not in present]


def is_real_review(body: str, author: str) -> bool:
    """Whether this is a review artifact from an account that reviews.

    Both halves are required. A marker alone is forgeable: the PR author
    can write "confidence score" in an ordinary comment and satisfy a
    marker-only test, which makes the gate assert something the author
    controls (CWE-345, Greptile on PR #1625). An author alone is not
    enough either, because a skip notice is posted by the same bot.

    Positive on both axes, so an unrecognised notice or an unexpected
    account fails closed. A review that found nothing still counts -- it
    ran -- which is why emptiness of findings cannot be the test.
    """
    if author.strip().lower() not in TRUSTED_REVIEWERS:
        return False
    lowered = body.strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in REVIEW_VERDICT_MARKERS)


def _author_login(artifact: dict[str, Any]) -> str:
    """The login that wrote a comment or review, across both payload shapes.

    `gh pr view --json comments` nests it as `author.login`; the REST
    comment payload uses `user.login`. Returning "" for an unrecognised
    shape makes the artifact fail the trusted-author test rather than
    silently pass it.
    """
    for key in ("author", "user"):
        holder = artifact.get(key)
        if isinstance(holder, dict):
            login = holder.get("login")
            if isinstance(login, str):
                return login
    return ""


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


def unresolved_in_page(page: dict[str, Any]) -> tuple[int, bool, str]:
    """`(unresolved on this page, has another page, end cursor)`.

    Split out so the pagination arithmetic is testable without the network,
    which is what makes the two-page fixture below a real test rather than
    a mock of the code under test.
    """
    threads = page["data"]["repository"]["pullRequest"]["reviewThreads"]
    nodes = threads["nodes"]
    unresolved = sum(1 for n in nodes if n.get("isResolved") is False)
    # Subscripted, not `.get(..., {})`. A page carrying `nodes` but no
    # `pageInfo` key would default to `hasNextPage: False` and be read as the
    # FINAL page, so the count came back complete with no error -- while every
    # other unreadable shape here routes to "unverified". That inconsistency
    # is the class this script exists to close: a missing key answered as a
    # clean result (CodeRabbit on PR #1625). The KeyError now takes the same
    # branch as the rest.
    info = threads["pageInfo"]
    # Required keys with type checks, not `.get`. `bool(info.get("hasNextPage"))`
    # is False for a `pageInfo` that exists but omits the field, so pagination
    # ended as if complete -- the same defect as the missing `pageInfo` above,
    # one level down, and I fixed the outer one without fixing the class
    # (CodeRabbit on PR #1625). A wrong TYPE is refused too: a string
    # "false" is truthy, so a shape change that turned the flag into a string
    # would silently invert this.
    has_next = info["hasNextPage"]
    cursor = info["endCursor"]
    if not isinstance(has_next, bool) or not isinstance(cursor, str | None):
        raise TypeError(f"unexpected pageInfo types: {type(has_next)}, {type(cursor)}")
    return unresolved, has_next, cursor or ""


def _unresolved_thread_count(pr: str) -> tuple[int, str]:
    """`(count, error)`. A non-empty error means the count is unverified.

    Paginated because `first: 100` silently truncates: an unresolved 101st
    thread is invisible, and PR #1503 carried 53, so the ceiling is within
    reach rather than theoretical. A page that cannot be read is reported
    rather than counted as zero.
    """
    total = 0
    cursor = ""
    for _ in range(20):
        after = f', after: "{cursor}"' if cursor else ""
        page = _json_dict(
            _gh_stdout_or_empty(
                "api",
                "graphql",
                "-f",
                "query="
                '{repository(owner:"vitali87",name:"code-graph-rag")'
                f"{{pullRequest(number:{pr})"
                f"{{reviewThreads(first: 100{after})"
                "{nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}",
            )
        )
        try:
            unresolved, has_next, cursor = unresolved_in_page(page)
        except (KeyError, TypeError, AttributeError):
            return (
                0,
                "could not read a page of review threads, so resolution is unverified",
            )
        total += unresolved
        if not has_next:
            return total, ""
    return (
        total,
        "review threads did not terminate after 20 pages; treating as unverified",
    )


def ci_runs_at_head(head: str) -> list[dict[str, Any]]:
    """The CI runs at exactly `head`, asked of the API by SHA.

    Listing recent runs and filtering client-side cannot answer this: the
    listing is a fixed-size window over the WHOLE repo, so on a busy repo a
    run drops out of it while the PR is still open and the PR then reports
    as having no CI run at all -- the "never ran" verdict, produced by a
    run that did happen and passed. Measured on #1826: its run was 36
    minutes old, every one of the 40 most recent runs was newer, and the
    tool called a fully green PR ungated.

    The `head_sha` parameter is exact, so age and repo traffic cannot
    affect the answer. It is NOT unbounded -- it pages at 30 by default,
    which is why this paginates; an unpaginated query would reintroduce
    the same bug once a SHA carried enough runs.

    The run is identified by workflow PATH rather than display name: a
    name is a string any workflow file may declare, so a second file
    named `CI` would satisfy a name check without running a test. The
    repo's own require-ci-at-head.yml matches on path for this reason.
    """
    raw = _gh_stdout_or_empty(
        "api",
        "--paginate",
        f"repos/{REPO}/actions/runs?head_sha={head}&per_page=100",
    )
    runs: list[dict[str, Any]] = []
    # `--paginate` without `--jq` concatenates one JSON object per page, so
    # this is a stream of objects rather than one document. `--jq` would
    # flatten it, but `gh` rejects the `--slurp` needed to rebuild an array
    # alongside `--jq`, so decode the pages here instead.
    decoder = json.JSONDecoder()
    index = 0
    while index < len(raw):
        # Skip separators BEFORE decoding, not only after, and do not
        # reorder these two steps. `raw_decode` does not tolerate leading
        # whitespace: skipping only after a successful decode means a
        # response opening with a newline raises on the first pass and
        # returns no runs at all, reported as "no CI run exists at the head
        # SHA" -- the exact false verdict this function was rewritten to
        # stop producing. Trailing-only skipping looks equivalent and is
        # not; `test_leading_whitespace_does_not_discard_every_page` fails
        # if these are swapped back.
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        try:
            page, offset = decoder.raw_decode(raw, index)
        except ValueError:
            break
        if isinstance(page, dict):
            found = page.get("workflow_runs", [])
            if isinstance(found, list):
                runs.extend(r for r in found if isinstance(r, dict))
        index = offset
    return [run for run in runs if str(run.get("path", "")) == CI_WORKFLOW_PATH]


# --- the classic branch-protection layer (issue #1957) -----------------------
#
# `rules/branches/<base>` returns RULESETS only. GitHub enforces classic branch
# protection as a second, independent layer, and `gh pr merge` is refused by
# whichever is stricter. Measured on this repo: the ruleset required 0
# approvals, the classic layer 1, and three fully green PRs (#1946, #1947,
# #1953) reported "gated" here and were then refused with "the base branch
# policy prohibits the merge". The endpoint 404s on a repo with no classic
# layer, which `_gh_stdout_or_empty` returns as "", so absence reads as "no
# classic layer", never as an error.


def classic_protection(base: str) -> dict[str, Any]:
    """The classic branch-protection layer on `base`, or {} when there is none."""
    return _json_dict(
        _gh_stdout_or_empty("api", f"repos/{REPO}/branches/{base}/protection")
    )


def ruleset_review_count(rules: list[Any]) -> int | None:
    """The approvals a `pull_request` ruleset rule requires; None without one."""
    counts = [
        int(count)
        for rule in rules
        if isinstance(rule, dict) and rule.get("type") == "pull_request"
        for count in [
            (rule.get("parameters") or {}).get("required_approving_review_count")
        ]
        if isinstance(count, int)
    ]
    return max(counts) if counts else None


def classic_review_count(protection: dict[str, Any]) -> int | None:
    """The approvals classic protection requires; None when it requires none."""
    reviews = protection.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        return None
    count = reviews.get("required_approving_review_count")
    return count if isinstance(count, int) else None


def classic_required_contexts(protection: dict[str, Any]) -> list[str]:
    checks = protection.get("required_status_checks")
    if not isinstance(checks, dict):
        return []
    contexts = checks.get("contexts")
    return [str(c) for c in contexts] if isinstance(contexts, list) else []


def approvals(reviews: list[Any]) -> int:
    """Distinct reviewers whose LATEST verdict is an approval.

    GitHub counts a reviewer's most recent non-comment review: an approval
    followed by "changes requested" is no longer an approval, and a
    comment-only review changes nothing. Bot accounts never satisfy a
    branch-policy approval requirement.
    """
    latest: dict[str, str] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state", "")).upper()
        if state in ("", "COMMENTED", "PENDING"):
            continue
        author = _author_login(review)
        if author.endswith("[bot]"):
            continue
        latest[author] = state
    return sum(1 for state in latest.values() if state == "APPROVED")


def merge_remedies() -> str:
    """Which routes past a refused merge exist on this repo, from its settings."""
    repo = _json_dict(_gh_stdout_or_empty("api", f"repos/{REPO}"))
    auto_merge = repo.get("allow_auto_merge")
    if auto_merge is True:
        return "auto-merge is enabled, so `--auto` will merge once the policy is met"
    if auto_merge is False:
        return "auto-merge is disabled on the repo, so `--auto` cannot help"
    return "the repo's auto-merge setting could not be read"


def approval_findings(
    pr: str,
    base: str,
    rules: list[Any],
    protection: dict[str, Any],
    reviews: list[Any],
) -> tuple[list[str], list[str]]:
    """Reasons and caveats from the approval requirement of BOTH layers."""
    reasons: list[str] = []
    caveats: list[str] = []
    from_ruleset = ruleset_review_count(rules)
    from_classic = classic_review_count(protection)
    if (
        from_ruleset is not None
        and from_classic is not None
        and from_ruleset != from_classic
    ):
        caveats.append(
            f"the ruleset and classic branch protection on '{base}' disagree on "
            f"approvals ({from_ruleset} vs {from_classic}); the higher one binds, "
            "and PRs that merged before the stricter layer was enabled are no "
            "guide to what merges now"
        )
    required = max(
        (n for n in (from_ruleset, from_classic) if n is not None), default=0
    )
    have = approvals(reviews)
    if have >= required:
        return reasons, caveats
    sources = [
        name
        for name, count in (
            ("classic branch protection", from_classic),
            ("ruleset", from_ruleset),
        )
        if count == required
    ]
    admins = protection.get("enforce_admins")
    enforced = isinstance(admins, dict) and admins.get("enabled") is True
    reasons.append(
        f"base '{base}' requires {required} approving review(s) "
        f"({' and '.join(sources)}) and #{pr} has {have}, so `gh pr merge` is "
        "refused with 'the base branch policy prohibits the merge'; "
        f"{merge_remedies()}; "
        + (
            "the rule is enforced for administrators too"
            if enforced
            else "`--admin` would bypass it, which is a decision, not a fix"
        )
    )
    return reasons, caveats


def check(pr: str) -> tuple[list[str], list[str]]:
    """Reasons `pr` is not verifiably gated, and caveats on the evidence.

    Reasons block: empty means gated. Caveats do not block -- they name
    evidence that is weaker than it looks, so a reader can weigh it. A
    review whose own checks could not execute is the caveat this exists
    for: its findings may be perfectly correct, but they rest on reasoning
    the reviewer could not confirm (#1824).
    """
    reasons: list[str] = []
    caveats: list[str] = []

    view = _json_dict(
        _gh_stdout_or_empty(
            "pr",
            "view",
            pr,
            "--repo",
            REPO,
            "--json",
            "headRefOid,baseRefName,statusCheckRollup,comments,reviews,state",
        )
    )
    if not view:
        return ([f"could not read PR #{pr} (is `gh` authenticated?)"], [])

    head = str(view.get("headRefOid", ""))
    base = str(view.get("baseRefName", ""))
    rollup = [e for e in view.get("statusCheckRollup", []) if isinstance(e, dict)]

    rules = _json_list(
        _gh_stdout_or_empty("api", f"repos/{REPO}/rules/branches/{base}")
    )
    rule_types = {r.get("type") for r in rules if isinstance(r, dict)}
    # Both enforcement layers (issue #1957): rulesets AND classic protection.
    protection = classic_protection(base)
    classic_contexts = classic_required_contexts(protection)
    if "required_status_checks" not in rule_types and not classic_contexts:
        reasons.append(
            f"base '{base}' is covered by no ruleset and no classic branch "
            "protection requiring status checks, so nothing is enforced on "
            "this PR regardless of its check list"
        )

    at_head = ci_runs_at_head(head)
    if not at_head:
        reasons.append(f"no CI run exists at the head SHA {head[:8]}")
    else:
        owners: set[str] = set()
        # Whether every run's detail was actually READ. `_gh_stdout_or_empty`
        # returns "" for a failed call and for an empty body alike, so an
        # unreadable run and a genuinely empty `pull_requests` are otherwise
        # indistinguishable -- and the closed-PR excuse below must never cover
        # the first. A run whose detail did not parse leaves this False.
        all_details_read = True
        for run in at_head:
            detail = _json_dict(
                _gh_stdout_or_empty("api", f"repos/{REPO}/actions/runs/{run.get('id')}")
            )
            associated = detail.get("pull_requests")
            if not detail or not isinstance(associated, list):
                # Unreadable, or readable but carrying no `pull_requests` FIELD.
                # A truthy detail that omits the key entirely is missing data,
                # not a cleared association: GitHub's own cleared form is
                # `"pull_requests": []`, a present-but-empty list (verified on
                # run 34874043546, a 35-key object). Treating an absent key as
                # cleared would excuse incomplete evidence on a closed PR.
                all_details_read = False
                continue
            owners.update(
                str(p.get("number")) for p in associated if isinstance(p, dict)
            )
        # GitHub CLEARS a run's `pull_requests` once its PR closes or merges
        # (issue #1944), so on a closed PR an empty owner set is GitHub's own
        # doing rather than an unanswered question. Reported as unverified it
        # made every correctly gated merged PR read as ungated -- measured on
        # #1930, whose merged head carries `pull_requests: []` while an open
        # PR's carries one entry, same repo and workflow.
        #
        # Only the EMPTY case is excused, and only when closed. A populated
        # set naming a different PR is the rebase collision the message
        # describes and still fails, whatever the state.
        state = str(view.get("state", "")).upper()
        # Excused only when the runs were READ and reported no owner. A failed
        # detail fetch keeps failing closed on a merged PR exactly as on an
        # open one: that is the fail-open the empty-is-unverified rule closed.
        cleared_by_close = (
            not owners and all_details_read and state in ("MERGED", "CLOSED")
        )
        if not all_details_read:
            # Checked BEFORE the owner match, not folded into it. A head can
            # carry several CI runs: if one resolves to this PR and another is
            # unreadable, `pr in owners` is true and the incomplete evidence
            # would never be reported. Ownership is a claim about EVERY run at
            # the head, so one unread run makes the answer unverified however
            # good the others look.
            reasons.append(
                f"could not read every CI run detail at {head[:8]}, so run "
                "ownership is unverified; an unread run may name another PR"
            )
        elif pr not in owners and not cleared_by_close:
            # Empty is UNVERIFIED, not clean, on an OPEN PR. A run whose detail
            # fetch failed, or one reporting `pull_requests: []`, leaves
            # `owners` empty; the earlier `if owners and ...` guard then never
            # fired and scored the absence of an answer as a pass (Greptile on
            # PR #1625) -- the same fail-open shape this checker exists to
            # catch.
            found = sorted(owners) if owners else "none (could not be determined)"
            reasons.append(
                f"the CI run at {head[:8]} does not resolve to #{pr}; owners: {found}. "
                "Branches sharing a head SHA after a rebase report each other's runs"
            )
        elif cleared_by_close:
            caveats.append(
                f"#{pr} is {state}, so GitHub has cleared its runs' "
                "`pull_requests`; run ownership could not be re-checked and is "
                "taken on trust here (issue #1944)"
            )

    missing = required_contexts_present(rollup, [REQUIRED_CONTEXT])
    if missing:
        reasons.append(
            f"required context absent at the head: {missing}; "
            + absent_context_reason(REQUIRED_CONTEXT, rollup, at_head)
        )
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

    # Contexts the CLASSIC layer requires are enforced exactly like the
    # ruleset's, and a PR missing one is refused the same way.
    for name in classic_contexts:
        if name == REQUIRED_CONTEXT:
            continue
        if required_contexts_present(rollup, [name]):
            reasons.append(
                f"context '{name}', required by classic branch protection, is "
                "absent at the head"
            )
            continue
        for entry in rollup:
            if context_name(entry) != name:
                continue
            if not is_concluded(entry):
                reasons.append(
                    f"'{name}' (required by classic branch protection) has not "
                    "concluded"
                )
            elif str(entry.get("conclusion", "")).upper() != "SUCCESS":
                reasons.append(
                    f"'{name}' (required by classic branch protection) concluded "
                    f"{entry.get('conclusion')}"
                )

    absent_jobs = missing_aggregated_jobs(rollup)
    if absent_jobs:
        reasons.append(
            f"jobs aggregated by '{REQUIRED_CONTEXT}' have no context at the head: "
            f"{absent_jobs}; a green aggregate over a set containing none of these "
            "reports on nothing"
        )

    artifacts = [
        (str(a.get("body", "")), _author_login(a))
        for key in ("comments", "reviews")
        for a in view.get(key, [])
        if isinstance(a, dict)
    ]
    real_reviews = [
        (body, author) for body, author in artifacts if is_real_review(body, author)
    ]
    if not real_reviews:
        reasons.append(
            f"no review artifact carries a verdict from a reviewing account "
            f"({len(artifacts)} comment(s)/review(s) present, none of which is a "
            "review by one of "
            f"{sorted(a for a in TRUSTED_REVIEWERS if not a.endswith('[bot]'))})"
        )
    else:
        caveats.extend(review_execution_caveats(real_reviews))

    caveats.extend(unrequired_failure_caveat(rollup))

    # The approval requirement of both layers, counted against the reviews
    # actually on the PR (issue #1957): the refusal it predicts is the one
    # `gh pr merge` prints without naming the layer.
    review_list = [r for r in view.get("reviews", []) if isinstance(r, dict)]
    approval_reasons, approval_caveats = approval_findings(
        pr, base, rules, protection, review_list
    )
    reasons.extend(approval_reasons)
    caveats.extend(approval_caveats)

    unresolved, thread_error = _unresolved_thread_count(pr)
    if thread_error:
        reasons.append(thread_error)
    elif unresolved:
        reasons.append(f"{unresolved} unresolved review thread(s)")

    return reasons, caveats


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        sys.stderr.write(f"usage: {argv[0]} <pr-number>\n")
        return 2
    pr = argv[1]
    reasons, caveats = check(pr)

    # Caveats print in BOTH outcomes, and above the verdict line when the PR
    # is otherwise gated. A caveat under a "gated" line is the one a reader
    # skips, which is the whole failure this reports on (#1824).
    for caveat in caveats:
        sys.stdout.write(f"  ! {caveat}\n")

    if not reasons:
        sys.stdout.write(
            f"PR #{pr}: gated ('{REQUIRED_CONTEXT}' present and satisfied; no "
            "other context was tested for being required"
            f"{'; see caveat(s) above' if caveats else ''})\n"
        )
        return 0
    sys.stdout.write(f"PR #{pr}: NOT verifiably gated -- {len(reasons)} reason(s)\n")
    for reason in reasons:
        sys.stdout.write(f"  - {reason}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
