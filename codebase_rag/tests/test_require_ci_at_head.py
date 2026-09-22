import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

from codebase_rag import constants as cs

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "require-ci-at-head.yml"
)
REPO = "vitali87/code-graph-rag"
FORK = "lllleolin-max/code-graph-rag"
HEAD = "287269496abaf96da59dbc9b3216310671773af6"
BRANCH = "fix-special-token-text-counting"
PR = 1865
PR_CREATED_AT = "2026-09-12T07:30:00Z"
RUN_CREATED_AT = "2026-09-12T07:36:19Z"
GH_STUB = r"""
gh() {
  if [[ "$1" == api && "$2" == --paginate &&
        "$3" == "repos/$REPO/pulls?state=all&head=lllleolin-max%3Afix-special-token-text-counting&per_page=100" &&
        "$4" == --jq ]]; then
    printf '%s\n' "$GH_PULL_REQUESTS" | jq "$5" || return
    return "${GH_PR_EXIT_CODE:-0}"
  fi
  if [[ "$1" != api || "$2" != --paginate ||
        "$3" != "repos/$REPO/actions/runs?head_sha=$HEAD_SHA&per_page=100" ||
        "$4" != --jq ]]; then
    echo 'Unexpected gh invocation' >&2
    return 97
  fi
  printf '%s\n' "$GH_PAGES" | jq "$5" || return
  return "${GH_EXIT_CODE:-0}"
}
"""


class Repository(TypedDict):
    full_name: str


class PullRequest(TypedDict):
    number: int


class PullRequestHead(TypedDict):
    ref: str
    sha: str
    repo: Repository | None


class SourcePullRequest(TypedDict, total=False):
    number: int
    created_at: str
    closed_at: str | None
    head: PullRequestHead


class WorkflowRun(TypedDict, total=False):
    name: str
    path: str
    head_sha: str
    head_branch: str
    head_repository: Repository | None
    repository: Repository
    pull_requests: list[PullRequest] | None
    status: str
    conclusion: str | None
    event: str
    created_at: str


def _make_workflow_run(
    *,
    repo: str = FORK,
    branch: str = BRANCH,
    sha: str = HEAD,
    prs: tuple[int, ...] = (),
    status: str = "completed",
    conclusion: str | None = "action_required",
) -> WorkflowRun:
    return WorkflowRun(
        name="CI",
        path=".github/workflows/ci.yml",
        head_sha=sha,
        head_branch=branch,
        head_repository=Repository(full_name=repo),
        repository=Repository(full_name=REPO),
        pull_requests=[PullRequest(number=number) for number in prs],
        status=status,
        conclusion=conclusion,
        event="pull_request",
        created_at=RUN_CREATED_AT,
    )


def _make_source_pull_request(number: int = PR) -> SourcePullRequest:
    return SourcePullRequest(
        number=number,
        created_at=PR_CREATED_AT,
        closed_at=None,
        head=PullRequestHead(ref=BRANCH, sha=HEAD, repo=Repository(full_name=FORK)),
    )


def _execute_workflow_check(
    pages: list[list[WorkflowRun]],
    *,
    head_repo: str = FORK,
    env: dict[str, str] | None = None,
    pr_pages: list[list[SourcePullRequest]] | None = None,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        # pytest.skip is not annotated NoReturn, so the checker cannot narrow
        # `bash` to str across it on its own.
        pytest.skip("the Ubuntu workflow requires bash and jq")
    assert bash is not None
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = workflow["jobs"]["require-ci-at-head"]["steps"][0]
    # Hand the script to bash as a FILE, never as a `-c` argument. On Windows
    # there is no execve: subprocess re-serialises argv into ONE command line,
    # and this script serialises to ~10.8k characters, past the 8191-char
    # limit. It is truncated mid-quote, so bash dies on an unbalanced quote
    # ("line 165: unexpected EOF while looking for matching `\"'") without ever
    # running a line of the step. A file path keeps the command line at a
    # constant ~90 characters however large the script grows.
    # newline="\n" pins LF endings so a CRLF checkout cannot leave a trailing
    # CR inside a token; belt-and-braces, as PyYAML already normalises the
    # block scalar's line breaks.
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "step.sh"
        script.write_text(
            GH_STUB + step["run"], encoding=cs.ENCODING_UTF8, newline="\n"
        )
        return _run_script(bash, script, head_repo, env, pages, pr_pages)


def _run_script(
    bash: str,
    script: Path,
    head_repo: str,
    env: dict[str, str] | None,
    pages: list[list[WorkflowRun]],
    pr_pages: list[list[SourcePullRequest]] | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, "--noprofile", "--norc", str(script)],
        env={
            **os.environ,
            "REPO": REPO,
            "HEAD_SHA": HEAD,
            "HEAD_BRANCH": BRANCH,
            "HEAD_REPO": head_repo,
            "PR_NUMBER": str(PR),
            "PR_CREATED_AT": PR_CREATED_AT,
            "GH_PAGES": "\n".join(
                json.dumps({"workflow_runs": runs}) for runs in pages
            ),
            "GH_EXIT_CODE": "0",
            "GH_PULL_REQUESTS": "\n".join(
                json.dumps(page)
                for page in (
                    pr_pages
                    if pr_pages is not None
                    else [[_make_source_pull_request()]]
                )
            ),
            "GH_PR_EXIT_CODE": "0",
            **(env or {}),
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=15,
    )


def test_fork_run_with_empty_pull_requests_is_present() -> None:
    result = _execute_workflow_check([[_make_workflow_run()]])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Found 1 'CI' run(s)" in result.stdout


@pytest.mark.parametrize("repo", [REPO, FORK])
@pytest.mark.parametrize("prs", [(PR,), (PR + 1, PR)])
def test_explicit_current_pr_association_is_present(
    repo: str, prs: tuple[int, ...]
) -> None:
    result = _execute_workflow_check(
        [[_make_workflow_run(repo=repo, prs=prs)]], head_repo=repo
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_same_repo_empty_association_is_not_proof() -> None:
    assert (
        _execute_workflow_check(
            [[_make_workflow_run(repo=REPO)]], head_repo=REPO
        ).returncode
        != 0
    )


@pytest.mark.parametrize("repo", [REPO, FORK])
def test_explicit_foreign_pr_is_rejected(repo: str) -> None:
    assert (
        _execute_workflow_check(
            [[_make_workflow_run(repo=repo, prs=(PR + 1,))]], head_repo=repo
        ).returncode
        != 0
    )


@pytest.mark.parametrize("prs", [(), (PR,)])
@pytest.mark.parametrize(
    "run",
    [
        _make_workflow_run(repo="another-contributor/code-graph-rag"),
        _make_workflow_run(repo=REPO),
        _make_workflow_run(branch="another-branch-at-the-same-sha"),
        _make_workflow_run(sha="a" * 40),
    ],
    ids=["another-fork", "base-repo", "another-branch", "another-sha"],
)
def test_run_source_must_match_even_with_pr_association(
    run: WorkflowRun, prs: tuple[int, ...]
) -> None:
    candidate = run.copy()
    candidate["pull_requests"] = [PullRequest(number=number) for number in prs]

    assert _execute_workflow_check([[candidate]]).returncode != 0


@pytest.mark.parametrize(
    "field", ["head_sha", "head_branch", "head_repository", "pull_requests", "path"]
)
def test_missing_run_identity_fails_closed(field: str) -> None:
    run = _make_workflow_run()
    run.pop(field)

    assert _execute_workflow_check([[run]]).returncode != 0


@pytest.mark.parametrize("field", ["head_repository", "pull_requests"])
def test_null_run_identity_fails_closed(field: str) -> None:
    run = _make_workflow_run()
    run[field] = None

    assert _execute_workflow_check([[run]]).returncode != 0


@pytest.mark.parametrize(
    "field", ["HEAD_SHA", "HEAD_BRANCH", "HEAD_REPO", "PR_NUMBER", "REPO"]
)
def test_missing_pr_identity_fails_closed(field: str) -> None:
    assert (
        _execute_workflow_check([[_make_workflow_run()]], env={field: ""}).returncode
        != 0
    )


def test_workflow_dispatch_without_pr_payload_fails_closed() -> None:
    result = _execute_workflow_check(
        [[_make_workflow_run()]],
        env={"HEAD_SHA": "", "HEAD_BRANCH": "", "HEAD_REPO": "", "PR_NUMBER": ""},
    )

    assert result.returncode != 0
    assert "No pull_request head SHA" in result.stdout


def test_workflow_display_name_cannot_impersonate_ci() -> None:
    run = _make_workflow_run(prs=(PR,))
    run["path"] = ".github/workflows/another.yml"

    assert _execute_workflow_check([[run]]).returncode != 0


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("queued", None),
        ("in_progress", None),
        ("completed", "success"),
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "action_required"),
    ],
)
def test_run_existence_does_not_replace_all_checks_pass(
    status: str, conclusion: str | None
) -> None:
    result = _execute_workflow_check(
        [[_make_workflow_run(status=status, conclusion=conclusion)]]
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_run_on_later_page_is_found() -> None:
    result = _execute_workflow_check(
        [[_make_workflow_run(branch="unrelated")] * 100, [_make_workflow_run()]]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Found 1 'CI' run(s)" in result.stdout


def test_no_runs_fails_closed() -> None:
    assert _execute_workflow_check([[]]).returncode != 0


def test_api_failure_after_partial_results_fails_closed() -> None:
    assert (
        _execute_workflow_check(
            [[_make_workflow_run()]], env={"GH_EXIT_CODE": "1"}
        ).returncode
        != 0
    )


def test_malformed_api_response_fails_closed() -> None:
    assert _execute_workflow_check([[]], env={"GH_PAGES": "not json"}).returncode != 0


def test_identity_comes_from_the_pull_request_event() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = workflow["jobs"]["require-ci-at-head"]["steps"][0]["env"]

    assert env["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert env["HEAD_BRANCH"] == "${{ github.event.pull_request.head.ref }}"
    assert env["HEAD_REPO"] == "${{ github.event.pull_request.head.repo.full_name }}"
    assert env["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert env["REPO"] == "${{ github.repository }}"


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("different_head", [False, True])
def test_another_pr_on_the_same_source_branch_is_ambiguous(
    closed: bool, different_head: bool
) -> None:
    other = _make_source_pull_request(PR + 1)
    if closed:
        other["closed_at"] = "2026-09-12T07:31:00Z"
    if different_head:
        other["head"]["sha"] = "a" * 40

    result = _execute_workflow_check(
        [[_make_workflow_run()]], pr_pages=[[_make_source_pull_request(), other]]
    )

    assert result.returncode != 0


def test_another_pr_with_the_same_source_cannot_inherit_the_run() -> None:
    result = _execute_workflow_check(
        [[_make_workflow_run()]], env={"PR_NUMBER": str(PR + 1)}
    )

    assert result.returncode != 0


def test_the_current_pr_head_must_still_match() -> None:
    pr = _make_source_pull_request()
    pr["head"]["sha"] = "a" * 40

    assert (
        _execute_workflow_check([[_make_workflow_run()]], pr_pages=[[pr]]).returncode
        != 0
    )


@pytest.mark.parametrize("different_field", ["repository", "branch"])
def test_unrelated_pr_sources_do_not_create_ambiguity(different_field: str) -> None:
    other = _make_source_pull_request(PR + 1)
    if different_field == "repository":
        other["head"]["repo"] = Repository(
            full_name="another-contributor/code-graph-rag"
        )
    else:
        other["head"]["ref"] = "another-branch"

    result = _execute_workflow_check(
        [[_make_workflow_run()]], pr_pages=[[_make_source_pull_request(), other]]
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_another_pr_on_a_later_page_still_creates_ambiguity() -> None:
    result = _execute_workflow_check(
        [[_make_workflow_run()]],
        pr_pages=[[_make_source_pull_request()], [_make_source_pull_request(PR + 1)]],
    )

    assert result.returncode != 0


def test_current_pr_on_a_later_page_is_found() -> None:
    other = _make_source_pull_request(PR + 1)
    other["head"]["ref"] = "another-branch"
    result = _execute_workflow_check(
        [[_make_workflow_run()]],
        pr_pages=[[other] * 100, [_make_source_pull_request()]],
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("field", ["number", "head", "created_at"])
def test_incomplete_pr_history_fails_closed(field: str) -> None:
    pr = _make_source_pull_request()
    pr.pop(field)

    assert (
        _execute_workflow_check([[_make_workflow_run()]], pr_pages=[[pr]]).returncode
        != 0
    )


@pytest.mark.parametrize("field", ["repo", "ref", "sha"])
def test_incomplete_pr_head_fails_closed(field: str) -> None:
    pr = _make_source_pull_request()
    pr["head"].pop(field)

    assert (
        _execute_workflow_check([[_make_workflow_run()]], pr_pages=[[pr]]).returncode
        != 0
    )


@pytest.mark.parametrize(
    "timestamp",
    ["", "invalid timestamp", "2026-09-12T07:37:00Z", "2026-02-30T07:30:00Z"],
)
def test_pr_creation_must_be_known_and_no_later_than_run_creation(
    timestamp: str,
) -> None:
    pr = _make_source_pull_request()
    pr["created_at"] = timestamp

    assert (
        _execute_workflow_check([[_make_workflow_run()]], pr_pages=[[pr]]).returncode
        != 0
    )


def test_missing_run_creation_fails_closed() -> None:
    run = _make_workflow_run()
    del run["created_at"]

    assert _execute_workflow_check([[run]]).returncode != 0


@pytest.mark.parametrize(
    "timestamp",
    ["", "invalid timestamp", "2026-09-12T07:29:00Z", "2026-09-31T07:30:00Z"],
)
def test_a_run_must_be_created_after_the_current_pr(timestamp: str) -> None:
    run = _make_workflow_run()
    run["created_at"] = timestamp

    assert _execute_workflow_check([[run]]).returncode != 0


@pytest.mark.parametrize(
    "event", ["", "push", "workflow_dispatch", "pull_request_target"]
)
def test_fork_fallback_requires_a_pull_request_event(event: str) -> None:
    run = _make_workflow_run()
    run["event"] = event

    assert _execute_workflow_check([[run]]).returncode != 0


@pytest.mark.parametrize("response", ["[]", "{}", "not json"])
def test_missing_or_malformed_pr_history_fails_closed(response: str) -> None:
    assert (
        _execute_workflow_check(
            [[_make_workflow_run()]], env={"GH_PULL_REQUESTS": response}
        ).returncode
        != 0
    )


def test_pr_api_failure_after_partial_results_fails_closed() -> None:
    assert (
        _execute_workflow_check(
            [[_make_workflow_run()]], env={"GH_PR_EXIT_CODE": "1"}
        ).returncode
        != 0
    )


def test_explicit_association_does_not_need_the_fork_history_fallback() -> None:
    assert (
        _execute_workflow_check(
            [[_make_workflow_run(prs=(PR,))]], env={"GH_PR_EXIT_CODE": "1"}
        ).returncode
        == 0
    )


@pytest.mark.parametrize("number", ["1865", 0, -1, 1865.5, True, None])
def test_malformed_pr_number_fails_closed(number: str | int | float | None) -> None:
    response = json.dumps(_make_source_pull_request())
    response = response.replace('"number": 1865', f'"number": {json.dumps(number)}')

    assert (
        _execute_workflow_check(
            [[_make_workflow_run()]], env={"GH_PULL_REQUESTS": f"[{response}]"}
        ).returncode
        != 0
    )


@pytest.mark.parametrize("malformed", ['""', "null", "1", "[]", "{}"])
def test_malformed_pr_repository_fails_closed(malformed: str) -> None:
    response = json.dumps(_make_source_pull_request())
    response = response.replace(json.dumps(FORK), malformed)

    assert (
        _execute_workflow_check(
            [[_make_workflow_run()]], env={"GH_PULL_REQUESTS": f"[{response}]"}
        ).returncode
        != 0
    )


def test_malformed_later_history_page_fails_closed() -> None:
    response = json.dumps([_make_source_pull_request()]) + "\n{}"

    assert (
        _execute_workflow_check(
            [[_make_workflow_run()]], env={"GH_PULL_REQUESTS": response}
        ).returncode
        != 0
    )


def test_a_later_pr_on_the_same_branch_still_creates_ambiguity() -> None:
    other = _make_source_pull_request(PR + 1)
    other["created_at"] = "2026-09-12T07:37:00Z"

    assert (
        _execute_workflow_check(
            [[_make_workflow_run()]], pr_pages=[[_make_source_pull_request(), other]]
        ).returncode
        != 0
    )


class TestTheFailureSaysWhichRunsWereExcludedAndWhy:
    """`ci_count == 0` has several causes; the message named only one.

    The gate prints every run it found, then asserted "No 'CI' workflow
    run matches" and recommended `gh workflow run` -- the remedy for the
    absent case. On a queued run that advice adds a second run without
    releasing the first, and the listing above contradicts the error
    (issue #1948). Each state now reports its own reason and its own
    remedy.
    """

    def test_a_queued_run_is_not_reported_as_absent(self) -> None:
        run = _make_workflow_run(status="queued", conclusion=None)
        result = _execute_workflow_check([[run]], pr_pages=[[]])

        assert result.returncode == 1
        assert "no ci.yml run exists" not in result.stdout
        assert "has not completed" in result.stdout
        assert "Do NOT dispatch another" in result.stdout
        assert "gh workflow run ci.yml" not in result.stdout

    def test_an_absent_run_still_gets_the_dispatch_remedy(self) -> None:
        result = _execute_workflow_check([[]])

        assert result.returncode == 1
        assert "no ci.yml run exists at this head" in result.stdout
        assert "gh workflow run ci.yml" in result.stdout

    def test_a_foreign_association_is_named_rather_than_denied(self) -> None:
        run = _make_workflow_run(prs=(9999,))
        result = _execute_workflow_check([[run]], pr_pages=[[]])

        assert result.returncode == 1
        # The REASON line, not a substring the static remedy also prints.
        # Asserting "not this PR" alone passed even with the whole reason
        # feature gutted, because the #1582 remedy text ends "it is not
        # this PR's evidence" (greptile-local on this branch).
        assert (
            "excluded because its PR association names [9999], not this PR"
            in result.stdout
        )
        # A completed run naming another PR means nothing is coming for
        # this one, so dispatching IS the right advice. This assertion
        # previously required its ABSENCE, which encoded the bug Greptile
        # found on #1955: a foreign run was treated as one to wait for.
        assert "gh workflow run ci.yml" in result.stdout

    def test_an_empty_association_names_the_failed_fallback(self) -> None:
        result = _execute_workflow_check([[_make_workflow_run()]], pr_pages=[[]])

        assert result.returncode == 1
        assert (
            "excluded because it carries no PR association and the "
            "source-branch fallback did not resolve it to this PR" in result.stdout
        )

    def test_a_foreign_source_branch_is_named(self) -> None:
        run = _make_workflow_run(branch="someone-else", prs=(9999,))
        result = _execute_workflow_check([[run]], pr_pages=[[]])

        assert result.returncode == 1
        assert (
            "excluded because it belongs to another source branch or repository"
            in result.stdout
        )

    def test_a_status_the_grep_would_miss_still_gets_the_pending_remedy(
        self,
    ) -> None:
        """`requested` and `pending` are real workflow-run statuses.

        The remedy was selected by grepping `status=(queued|waiting|
        in_progress)` out of the rendered reason text, so these two fell
        through to the absent-case advice and told the reader to dispatch
        a run that already existed.
        """
        run = _make_workflow_run(status="requested", conclusion=None)
        result = _execute_workflow_check([[run]], pr_pages=[[]])

        assert result.returncode == 1
        assert "has not completed" in result.stdout
        assert "gh workflow run ci.yml" not in result.stdout

    def test_a_foreign_queued_run_does_not_claim_one_is_coming(self) -> None:
        """The shared-head case (#1582): every run belongs to another PR.

        Selecting the remedy by grepping for any queued run printed "Do
        NOT dispatch another" even though no run for THIS PR was coming,
        which is the one situation where dispatching is the right advice.
        """
        runs = [
            _make_workflow_run(
                branch="other-branch", prs=(9999,), status="queued", conclusion=None
            ),
            _make_workflow_run(
                branch="other-branch", prs=(9999,), conclusion="failure"
            ),
        ]
        result = _execute_workflow_check([runs], pr_pages=[[]])

        assert result.returncode == 1
        assert "Do NOT dispatch another" not in result.stdout

    def test_a_queued_run_on_this_branch_for_another_pr_is_not_awaited(
        self,
    ) -> None:
        """Matching source identity is not enough to be worth waiting for.

        A run can match this PR's SHA, branch AND repository while being
        associated with a different PR -- `ci_count` excludes it for the
        association, so it can never satisfy this gate no matter how long
        it runs. Filtering `ci_state` on source identity alone called it
        `pending` and told the contributor not to dispatch, leaving the
        PR with no eligible run and no instruction to create one
        (Greptile on #1955).

        An EMPTY association is different and must still count as
        pending: the fork fallback may yet resolve it to this PR.
        """
        run = _make_workflow_run(prs=(9999,), status="queued", conclusion=None)
        result = _execute_workflow_check([[run]], pr_pages=[[]])

        assert result.returncode == 1
        assert "Do NOT dispatch another" not in result.stdout
        assert "gh workflow run ci.yml" in result.stdout

    def test_a_queued_fork_run_with_no_association_is_still_awaited(
        self,
    ) -> None:
        """The control for the test above: on a FORK an empty association
        may still resolve through the source-branch fallback, so it stays
        `pending` and must NOT be told to dispatch.

        `repo`/`head_repo` are passed explicitly rather than relying on
        `_make_workflow_run`'s FORK default, because the repository is the
        axis that decides this case against the same-repo test below.
        """
        run = _make_workflow_run(repo=FORK, status="queued", conclusion=None)
        result = _execute_workflow_check([[run]], head_repo=FORK, pr_pages=[[]])

        assert result.returncode == 1
        assert "Do NOT dispatch another" in result.stdout
        assert "gh workflow run ci.yml" not in result.stdout

    def test_a_queued_same_repo_run_with_no_association_is_not_awaited(
        self,
    ) -> None:
        """A same-repo run with an empty association can never count.

        `ci_count`'s empty-association fallback is gated on
        `HEAD_REPO != REPO`, so for a branch in this repository nothing
        will ever associate the run with this PR
        (`test_same_repo_empty_association_is_not_proof` refuses it as
        proof). Classifying it `pending` printed "Do NOT dispatch
        another" and left the PR with no eligible run and no way to get
        one (Copilot on #1955).

        The exact mirror of the fork test above: same status, same empty
        association, only the repository differs, and the remedy must
        differ with it.
        """
        run = _make_workflow_run(repo=REPO, status="queued", conclusion=None)
        result = _execute_workflow_check([[run]], head_repo=REPO, pr_pages=[[]])

        assert result.returncode == 1
        assert "Do NOT dispatch another" not in result.stdout
        assert "gh workflow run ci.yml" in result.stdout

    def test_a_queued_push_event_run_is_not_awaited(self) -> None:
        """`ci_count`'s fallback requires `.event == "pull_request"`.

        A `push`-event run can never gain a PR association, so awaiting
        it withholds the dispatch command for ever.
        """
        run = _make_workflow_run(status="queued", conclusion=None)
        run["event"] = "push"
        result = _execute_workflow_check(
            [[run]], pr_pages=[[_make_source_pull_request()]]
        )

        assert result.returncode == 1
        assert "Do NOT dispatch another" not in result.stdout
        assert "gh workflow run ci.yml" in result.stdout

    def test_a_queued_run_created_before_the_pr_is_not_awaited(self) -> None:
        """A run's `created_at` never changes, so a run predating the PR
        can never satisfy the fallback's `created_at >= pr_created_at`."""
        run = _make_workflow_run(status="queued", conclusion=None)
        run["created_at"] = "2026-09-01T00:00:00Z"
        result = _execute_workflow_check(
            [[run]], pr_pages=[[_make_source_pull_request()]]
        )

        assert result.returncode == 1
        assert "Do NOT dispatch another" not in result.stdout
        assert "gh workflow run ci.yml" in result.stdout

    def test_an_unresolved_history_says_the_wait_may_not_clear(self) -> None:
        """Ambiguous history stays awaitable -- it can settle when a PR
        sharing the branch closes -- but the step must not say only "Do
        NOT dispatch" after reporting the history did not resolve.
        """
        run = _make_workflow_run(status="queued", conclusion=None)
        result = _execute_workflow_check(
            [[run]],
            pr_pages=[[_make_source_pull_request(), _make_source_pull_request(PR + 1)]],
        )

        assert result.returncode == 1
        assert "does not uniquely identify" in result.stdout
        assert "cannot be counted YET" in result.stdout
        assert "fresh branch name" in result.stdout

    def test_a_resolved_history_omits_the_unresolved_advice(self) -> None:
        """Control for the test above: when the fallback DID resolve, the
        extra paragraph must not print."""
        run = _make_workflow_run(status="queued", conclusion=None)
        run["created_at"] = "2026-09-01T00:00:00Z"
        result = _execute_workflow_check(
            [[run]], pr_pages=[[_make_source_pull_request()]]
        )

        assert "cannot be counted YET" not in result.stdout

    def test_a_foreign_association_routes_to_the_dispatch_remedy(self) -> None:
        """A run naming another PR is not awaitable, so the state is
        `absent`. The `foreign` branch carried two lines describing this
        case that could never print."""
        run = _make_workflow_run(prs=(9999,), conclusion="success")
        result = _execute_workflow_check([[run]], pr_pages=[[]])

        assert result.returncode == 1
        assert "gh workflow run ci.yml" in result.stdout
        assert "naming a different PR" not in result.stdout

    def test_a_branch_named_like_a_status_does_not_flip_the_remedy(self) -> None:
        """`head_branch` is interpolated into the reason text the grep
        then scanned, so a branch called `status=queued` chose the
        remedy."""
        run = _make_workflow_run(
            branch="status=queued", prs=(9999,), conclusion="success"
        )
        result = _execute_workflow_check([[run]], pr_pages=[[]])

        assert result.returncode == 1
        assert "Do NOT dispatch another" not in result.stdout
