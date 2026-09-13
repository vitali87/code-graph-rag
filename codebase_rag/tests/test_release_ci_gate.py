import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
)
REPO = "vitali87/code-graph-rag"
SHA = "58670b840701c5c4212ee20d622705548da1c164"
WORKFLOW_ID = 229278790
GATE_NAME = "Require successful CI for the release source"
STUBS = r"""
git() {
  if [[ "$*" == 'rev-parse HEAD^' ]]; then
    printf '%s\n' "$PARENT_SHA"
  else
    printf 'git: %s\n' "$*"
  fi
}
gh() {
  if [[ "$1" == api &&
        "$2" == "repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml" &&
        "$3" == --jq ]]; then
    printf '%s\n' "$GH_WORKFLOW" | jq "$4" || return
    return "${GH_WORKFLOW_EXIT:-0}"
  fi
  if [[ "$1" != api || "$2" != --paginate || "$3" != --slurp ||
        "$4" != "repos/$GITHUB_REPOSITORY/actions/workflows/$EXPECTED_WORKFLOW_ID/runs?head_sha=$GITHUB_SHA&event=push&branch=main&per_page=100" ]]; then
    echo 'Unexpected gh invocation' >&2
    return 97
  fi
  printf '%s\n' "$GH_PAGES"
  return "${GH_RUNS_EXIT:-0}"
}
sleep() {
  printf 'wait: %s\n' "$*"
  GH_PAGES="${GH_NEXT_PAGES:-$GH_PAGES}"
}
"""


class Repository(TypedDict):
    full_name: str


class WorkflowRun(TypedDict, total=False):
    id: int
    workflow_id: int
    run_attempt: int
    name: str
    path: str
    head_sha: str
    head_branch: str
    head_repository: Repository | None
    repository: Repository | None
    status: str
    conclusion: str | None
    event: str


class Step(TypedDict, total=False):
    name: str
    run: str
    env: dict[str, str | int]


def _make_ci_run(**updates: str | int | None | Repository) -> WorkflowRun:
    run = WorkflowRun(
        id=34741154226,
        workflow_id=WORKFLOW_ID,
        run_attempt=1,
        name="CI",
        path=".github/workflows/ci.yml",
        head_sha=SHA,
        head_branch="main",
        head_repository=Repository(full_name=REPO),
        repository=Repository(full_name=REPO),
        status="completed",
        conclusion="success",
        event="push",
    )
    run.update(updates)
    return run


def _pages(*runs: list[WorkflowRun]) -> str:
    count = sum(map(len, runs))
    return json.dumps([dict(total_count=count, workflow_runs=page) for page in runs])


def _steps() -> list[Step]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["bump-version"][
        "steps"
    ]


def _execute(
    pages: str,
    *,
    release: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("the Ubuntu workflow requires bash and jq")
    steps = _steps()
    names = [step["name"] for step in steps]
    scripts = []
    for step in steps[
        names.index("Commit version bump") : names.index("Create git tag") + 1
    ]:
        condition = step.get("if")
        if condition == "steps.decide.outputs.release == 'true'" and not release:
            continue
        assert condition in {
            "steps.check_manual.outputs.skip == 'false'",
            "steps.decide.outputs.release == 'true'",
        }
        scripts.append(
            "(\n"
            + step["run"]
            .replace("${{ steps.bump_version.outputs.new }}", "0.0.951")
            .replace("${{ steps.decide.outputs.release }}", str(release).lower())
            + "\n)\n"
        )
    return subprocess.run(
        [
            bash,
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            STUBS + "\n".join(scripts),
        ],
        env={
            **os.environ,
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": SHA,
            "GITHUB_TOKEN": "unused-test-token",
            "PARENT_SHA": SHA,
            "EXPECTED_WORKFLOW_ID": str(WORKFLOW_ID),
            "CI_WAIT_ATTEMPTS": "2",
            "GH_WORKFLOW": json.dumps(
                dict(
                    id=WORKFLOW_ID,
                    name="CI",
                    path=".github/workflows/ci.yml",
                    state="active",
                )
            ),
            "GH_PAGES": pages,
            "GH_RUNS_EXIT": "0",
            "GH_WORKFLOW_EXIT": "0",
            "GH_NEXT_PAGES": "",
            **(env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _assert_blocked(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, result.stdout + result.stderr
    assert "git: commit -m chore: bump version to 0.0.951" in result.stdout
    assert "git: push\n" in result.stdout
    assert "git: tag " not in result.stdout
    assert "git: push origin v" not in result.stdout


def test_successful_ci_releases_the_pushed_bump() -> None:
    result = _execute(_pages([_make_ci_run()]))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git: push origin v0.0.951" in result.stdout
    assert "wait:" not in result.stdout


def test_interim_tag_does_not_wait_for_ci() -> None:
    result = _execute(_pages([]), release=False, env={"GH_RUNS_EXIT": "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git: push https://x-access-token:" in result.stdout
    assert "wait:" not in result.stdout


@pytest.mark.parametrize(
    "conclusion",
    [
        "failure",
        "cancelled",
        "timed_out",
        "skipped",
        "neutral",
        "action_required",
        "stale",
        None,
    ],
)
def test_unsuccessful_ci_keeps_the_bump_but_blocks_the_tag(
    conclusion: str | None,
) -> None:
    _assert_blocked(_execute(_pages([_make_ci_run(conclusion=conclusion)])))


@pytest.mark.parametrize(
    "status", ["queued", "in_progress", "pending", "waiting", "requested"]
)
def test_unfinished_ci_cannot_release(status: str) -> None:
    _assert_blocked(_execute(_pages([_make_ci_run(status=status, conclusion=None)])))


@pytest.mark.parametrize(
    "initial", [[], [_make_ci_run(status="queued", conclusion=None)]]
)
def test_delayed_ci_can_finish_successfully(initial: list[WorkflowRun]) -> None:
    result = _execute(_pages(initial), env={"GH_NEXT_PAGES": _pages([_make_ci_run()])})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "wait: 30" in result.stdout
    assert "git: push origin v0.0.951" in result.stdout


def test_no_run_times_out_without_tagging() -> None:
    result = _execute(_pages([]))

    _assert_blocked(result)
    assert "Timed out" in result.stdout


@pytest.mark.parametrize(
    "run",
    [
        _make_ci_run(head_sha="a" * 40),
        _make_ci_run(head_branch="feature"),
        _make_ci_run(
            head_repository=Repository(full_name="contributor/code-graph-rag")
        ),
        _make_ci_run(repository=Repository(full_name="contributor/code-graph-rag")),
        _make_ci_run(event="pull_request"),
        _make_ci_run(event="workflow_dispatch"),
        _make_ci_run(path=".github/workflows/unrelated.yml"),
        _make_ci_run(workflow_id=WORKFLOW_ID + 1),
    ],
    ids=[
        "sha",
        "branch",
        "head-repo",
        "run-repo",
        "pr",
        "dispatch",
        "path",
        "workflow",
    ],
)
def test_unrelated_green_run_is_not_release_evidence(run: WorkflowRun) -> None:
    _assert_blocked(_execute(_pages([run])))


@pytest.mark.parametrize(
    "field",
    [
        "head_sha",
        "head_branch",
        "head_repository",
        "repository",
        "event",
        "path",
        "workflow_id",
        "id",
        "run_attempt",
    ],
)
def test_missing_run_identity_fails_closed(field: str) -> None:
    run = _make_ci_run()
    run.pop(field)

    _assert_blocked(_execute(_pages([run])))


@pytest.mark.parametrize(
    "status, conclusion", [("completed", "failure"), ("in_progress", None)]
)
@pytest.mark.parametrize("rerun", [False, True])
def test_newer_run_or_attempt_overrides_old_success(
    status: str, conclusion: str | None, rerun: bool
) -> None:
    newer = _make_ci_run(
        id=34741154226 if rerun else 34741154227,
        run_attempt=2 if rerun else 1,
        status=status,
        conclusion=conclusion,
    )

    _assert_blocked(_execute(_pages([newer], [_make_ci_run()])))


def test_success_on_later_page_is_accepted() -> None:
    result = _execute(_pages([_make_ci_run(head_sha="b" * 40)], [_make_ci_run()]))

    assert result.returncode == 0, result.stdout + result.stderr


def test_successful_rerun_supersedes_old_failure() -> None:
    result = _execute(
        _pages([_make_ci_run(conclusion="failure")], [_make_ci_run(run_attempt=2)])
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_ci_that_fails_after_waiting_blocks_the_tag() -> None:
    result = _execute(
        _pages([_make_ci_run(status="in_progress", conclusion=None)]),
        env={"GH_NEXT_PAGES": _pages([_make_ci_run(conclusion="failure")])},
    )

    _assert_blocked(result)
    assert "wait: 30" in result.stdout


def test_incomplete_pagination_cannot_authorize_a_release() -> None:
    pages = json.dumps([dict(total_count=2, workflow_runs=[_make_ci_run()])])

    _assert_blocked(_execute(pages))


@pytest.mark.parametrize("field", ["GH_WORKFLOW_EXIT", "GH_RUNS_EXIT"])
def test_api_failure_cannot_authorize_a_release_even_with_success_output(
    field: str,
) -> None:
    _assert_blocked(_execute(_pages([_make_ci_run()]), env={field: "1"}))


@pytest.mark.parametrize(
    "pages",
    [
        "broken json",
        "null",
        "[]",
        "{}",
        '[{"message":"Forbidden"}]',
        '[{"workflow_runs":null}]',
    ],
)
def test_malformed_responses_fail_closed(pages: str) -> None:
    _assert_blocked(_execute(pages))


@pytest.mark.parametrize(
    "workflow",
    [
        dict(
            id=WORKFLOW_ID,
            name="CI",
            path=".github/workflows/other.yml",
            state="active",
        ),
        dict(
            id=WORKFLOW_ID,
            name="CI",
            path=".github/workflows/ci.yml",
            state="disabled_manually",
        ),
        dict(id=None, name="CI", path=".github/workflows/ci.yml", state="active"),
    ],
)
def test_workflow_identity_must_be_verified(
    workflow: dict[str, str | int | None],
) -> None:
    _assert_blocked(
        _execute(_pages([_make_ci_run()]), env={"GH_WORKFLOW": json.dumps(workflow)})
    )


@pytest.mark.parametrize(
    "env", [{"PARENT_SHA": "b" * 40}, {"GITHUB_REF": "refs/heads/feature"}]
)
def test_release_source_must_be_the_bump_parent_on_main(env: dict[str, str]) -> None:
    _assert_blocked(_execute(_pages([_make_ci_run()]), env=env))


def test_gate_is_required_between_bump_push_and_release_side_effects() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["bump-version"]
    steps = job["steps"]
    names = [step["name"] for step in steps]
    gate = steps[names.index(GATE_NAME)]

    assert names.index("Commit version bump") < names.index(GATE_NAME)
    assert (
        names.index(GATE_NAME)
        < names.index("Create git tag")
        < names.index("Create release")
    )
    assert gate["if"] == "steps.decide.outputs.release == 'true'"
    assert not gate.get("continue-on-error", False)
    assert gate["timeout-minutes"] == 50
    assert gate["env"]["CI_WAIT_ATTEMPTS"] == 90
    assert job["timeout-minutes"] >= 90
    assert job["permissions"]["actions"] == "read"
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "workflow_run" not in workflow.get("on", workflow.get(True, {}))
