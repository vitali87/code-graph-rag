import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

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
GH_STUB = r"""
gh() {
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


def _run(
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
    )


def _check(
    pages: list[list[WorkflowRun]],
    *,
    head_repo: str = FORK,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("the Ubuntu workflow requires bash and jq")
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = workflow["jobs"]["require-ci-at-head"]["steps"][0]
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-c", GH_STUB + step["run"]],
        env={
            **os.environ,
            "REPO": REPO,
            "HEAD_SHA": HEAD,
            "HEAD_BRANCH": BRANCH,
            "HEAD_REPO": head_repo,
            "PR_NUMBER": str(PR),
            "GH_PAGES": "\n".join(
                json.dumps({"workflow_runs": runs}) for runs in pages
            ),
            "GH_EXIT_CODE": "0",
            **(env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_fork_run_with_empty_pull_requests_is_present() -> None:
    result = _check([[_run()]])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Found 1 'CI' run(s)" in result.stdout


@pytest.mark.parametrize("repo", [REPO, FORK])
@pytest.mark.parametrize("prs", [(PR,), (PR + 1, PR)])
def test_explicit_current_pr_association_is_present(
    repo: str, prs: tuple[int, ...]
) -> None:
    result = _check([[_run(repo=repo, prs=prs)]], head_repo=repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_same_repo_empty_association_is_not_proof() -> None:
    assert _check([[_run(repo=REPO)]], head_repo=REPO).returncode != 0


@pytest.mark.parametrize("repo", [REPO, FORK])
def test_explicit_foreign_pr_is_rejected(repo: str) -> None:
    assert _check([[_run(repo=repo, prs=(PR + 1,))]], head_repo=repo).returncode != 0


@pytest.mark.parametrize("prs", [(), (PR,)])
@pytest.mark.parametrize(
    "run",
    [
        _run(repo="another-contributor/code-graph-rag"),
        _run(repo=REPO),
        _run(branch="another-branch-at-the-same-sha"),
        _run(sha="a" * 40),
    ],
    ids=["another-fork", "base-repo", "another-branch", "another-sha"],
)
def test_run_source_must_match_even_with_pr_association(
    run: WorkflowRun, prs: tuple[int, ...]
) -> None:
    candidate = run.copy()
    candidate["pull_requests"] = [PullRequest(number=number) for number in prs]

    assert _check([[candidate]]).returncode != 0


@pytest.mark.parametrize(
    "field", ["head_sha", "head_branch", "head_repository", "pull_requests", "path"]
)
def test_missing_run_identity_fails_closed(field: str) -> None:
    run = _run()
    run.pop(field)

    assert _check([[run]]).returncode != 0


@pytest.mark.parametrize("field", ["head_repository", "pull_requests"])
def test_null_run_identity_fails_closed(field: str) -> None:
    run = _run()
    run[field] = None

    assert _check([[run]]).returncode != 0


@pytest.mark.parametrize(
    "field", ["HEAD_SHA", "HEAD_BRANCH", "HEAD_REPO", "PR_NUMBER", "REPO"]
)
def test_missing_pr_identity_fails_closed(field: str) -> None:
    assert _check([[_run()]], env={field: ""}).returncode != 0


def test_workflow_dispatch_without_pr_payload_fails_closed() -> None:
    result = _check(
        [[_run()]],
        env={"HEAD_SHA": "", "HEAD_BRANCH": "", "HEAD_REPO": "", "PR_NUMBER": ""},
    )

    assert result.returncode != 0
    assert "No pull_request head SHA" in result.stdout


def test_workflow_display_name_cannot_impersonate_ci() -> None:
    run = _run(prs=(PR,))
    run["path"] = ".github/workflows/another.yml"

    assert _check([[run]]).returncode != 0


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
    result = _check([[_run(status=status, conclusion=conclusion)]])

    assert result.returncode == 0, result.stdout + result.stderr


def test_run_on_later_page_is_found() -> None:
    result = _check([[_run(branch="unrelated")] * 100, [_run()]])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Found 1 'CI' run(s)" in result.stdout


def test_no_runs_fails_closed() -> None:
    assert _check([[]]).returncode != 0


def test_api_failure_after_partial_results_fails_closed() -> None:
    assert _check([[_run()]], env={"GH_EXIT_CODE": "1"}).returncode != 0


def test_malformed_api_response_fails_closed() -> None:
    assert _check([[]], env={"GH_PAGES": "not json"}).returncode != 0


def test_identity_comes_from_the_pull_request_event() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = workflow["jobs"]["require-ci-at-head"]["steps"][0]["env"]

    assert env["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert env["HEAD_BRANCH"] == "${{ github.event.pull_request.head.ref }}"
    assert env["HEAD_REPO"] == "${{ github.event.pull_request.head.repo.full_name }}"
    assert env["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert env["REPO"] == "${{ github.repository }}"
