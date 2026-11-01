import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "deferred-release.yml"
BUMP_WORKFLOW = WORKFLOWS / "version-bump.yml"
REPO = "vitali87/code-graph-rag"
BOT = "github-actions[bot]"
# Two digits in the patch number: a lexical sort would rank v0.4.9 above it.
TAG = "v0.4.10"
LATEST = "v0.4.9"
TAG_OBJECT = "a" * 40
COMMIT = "b" * 40
MARKER = yaml.safe_load(BUMP_WORKFLOW.read_text(encoding="utf-8"))["env"][
    "DEFERRED_RELEASE_MARKER"
]
DEFERRED_TAGS = (
    f"{'d' * 40}\trefs/tags/{LATEST}\n"
    f"{TAG_OBJECT}\trefs/tags/{TAG}\n"
    f"{COMMIT}\trefs/tags/{TAG}^{{}}\n"
)
LIGHTWEIGHT_TAGS = f"{'d' * 40}\trefs/tags/{LATEST}\n{COMMIT}\trefs/tags/{TAG}\n"
PUSHED = (
    f"git push --force-with-lease=refs/tags/{TAG}:{TAG_OBJECT}"
    f" origin {COMMIT}:refs/tags/{TAG}"
)
PUBLISHED = (
    f"gh api -X PATCH repos/{REPO}/releases/9 -F draft=false -f make_latest=true"
)

# gh answers from JSON fixtures through the real jq, so the filters the
# workflow passes are exercised too. Every call lands in `calls`.
STUBS = r"""
gh() {
  printf 'gh %s\n' "$*" >> calls
  local url="" filter="" method=GET previous="" fixture
  for arg in "$@"; do
    case "$previous" in
      --jq) filter="$arg" ;;
      -X) method="$arg" ;;
    esac
    case "$arg" in repos/*) url="${arg#repos/"$GITHUB_REPOSITORY"/}" ;; esac
    previous="$arg"
  done
  case "$method $url" in
    "GET releases/latest") fixture=latest.json ;;
    "GET releases?per_page=100") fixture=releases.json ;;
    "GET git/tags/"*) fixture=annotation.json ;;
    PATCH*) return 0 ;;
    *) return 1 ;;
  esac
  [ -f "$fixture" ] || return 1
  jq -r "$filter" "$fixture"
}
git() {
  printf 'git %s\n' "$*" >> calls
  if [ "$1" = ls-remote ]; then cat tags; fi
}
python3() {
  printf 'python3 %s\n' "$*" >> calls
  return "${CI_EXIT:-0}"
}
"""


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _step(path: Path, job: str, name: str) -> dict:
    return next(
        step
        for step in _workflow(path)["jobs"][job]["steps"]
        if step.get("name") == name
    )


def _release(
    release_id: int, tag: str, *, draft: bool = True, author: str = BOT
) -> dict[str, int | str | bool | dict[str, str]]:
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "author": {"login": author},
    }


def _run(
    tmp_path: Path, script: str, files: dict[str, str], **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("the Ubuntu workflow requires bash and jq")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
    (tmp_path / "step.sh").write_text(
        STUBS + "\n" + script, encoding=cs.ENCODING_UTF8, newline="\n"
    )
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "./step.sh"],
        cwd=tmp_path,
        env={
            **os.environ,
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "DEFERRED_RELEASE_MARKER": MARKER,
            **env,
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=15,
    )
    calls = tmp_path / "calls"
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return result, lines


def _publish(
    tmp_path: Path,
    *,
    tags: str = DEFERRED_TAGS,
    releases: list | None = None,
    latest: str | None = LATEST,
    annotation: str | None = f"{MARKER}: CI is not proven for {'c' * 40}",
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    files = {
        "tags": tags,
        "releases.json": json.dumps(
            [_release(9, TAG)] if releases is None else releases
        ),
    }
    if latest is not None:
        files["latest.json"] = json.dumps({"tag_name": latest})
    if annotation is not None:
        files["annotation.json"] = json.dumps({"message": annotation})
    script = _step(WORKFLOW, "publish", "Publish the deferred release")["run"]
    return _run(tmp_path, script, files, **env)


def _assert_nothing_published(calls: list[str]) -> None:
    assert not [call for call in calls if call.startswith("git push")], calls
    assert not [call for call in calls if "-X PATCH" in call], calls


def test_a_deferred_release_is_published_once_ci_proves_it(tmp_path: Path) -> None:
    result, calls = _publish(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"python3 scripts/require_green_ci.py refs/tags/{TAG}" in calls
    # The plain tag pushed over the deploy key is what starts publish.yml,
    # docker-publish.yml and build-binaries.yml, as on a release that was
    # never deferred; the draft goes public right after it.
    assert calls.index(PUSHED) < calls.index(PUBLISHED)


def test_a_retry_after_the_tag_was_replaced_publishes_the_draft(
    tmp_path: Path,
) -> None:
    result, calls = _publish(tmp_path, tags=LIGHTWEIGHT_TAGS)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not [call for call in calls if call.startswith("git push")], calls
    assert PUBLISHED in calls


def test_unproven_ci_leaves_the_release_deferred(tmp_path: Path) -> None:
    result, calls = _publish(tmp_path, CI_EXIT="1")

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_nothing_published(calls)


@pytest.mark.parametrize(
    "case",
    [
        {"latest": TAG},
        {"releases": []},
        {"releases": [_release(9, TAG, author="vitali87")]},
        {"releases": [_release(9, TAG, draft=False)]},
        {"releases": [_release(8, "v0.4.8")]},
    ],
    ids=["released", "no-draft", "person's-draft", "published", "older-draft"],
)
def test_nothing_is_published_without_a_deferred_draft_for_the_newest_tag(
    tmp_path: Path, case: dict
) -> None:
    result, calls = _publish(tmp_path, **case)

    assert result.returncode == 0, result.stdout + result.stderr
    _assert_nothing_published(calls)


def test_a_tag_not_annotated_as_deferred_is_never_replaced(tmp_path: Path) -> None:
    result, calls = _publish(tmp_path, annotation=f"{TAG}, tagged by hand")

    assert result.returncode != 0
    _assert_nothing_published(calls)


@pytest.mark.parametrize("missing", ["latest", "annotation"])
def test_state_that_cannot_be_read_publishes_nothing(
    tmp_path: Path, missing: str
) -> None:
    result, calls = _publish(tmp_path, **{missing: None})

    assert result.returncode != 0
    _assert_nothing_published(calls)


def test_only_a_green_ci_run_for_a_push_to_main_starts_it() -> None:
    workflow = _workflow(WORKFLOW)
    triggers = workflow.get("on", workflow.get(True, {}))
    job = workflow["jobs"]["publish"]

    assert triggers.get("workflow_run") == {
        "workflows": ["CI"],
        "types": ["completed"],
        "branches": ["main"],
    }
    for condition in (
        "github.ref == 'refs/heads/main'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert condition in job.get("if", ""), condition


def test_it_takes_its_turn_with_the_bump_job() -> None:
    # A bump run in progress may already have decided to release; publishing
    # beside it would ship two releases for one deferred change.
    bump = _workflow(BUMP_WORKFLOW)["jobs"]["bump-version"]

    assert (
        _workflow(WORKFLOW)["jobs"]["publish"].get("concurrency") == bump["concurrency"]
    )


def test_it_reads_the_marker_the_bump_job_writes() -> None:
    workflow = _workflow(WORKFLOW)
    step = _step(WORKFLOW, "publish", "Publish the deferred release")

    assert workflow.get("env", {}).get("DEFERRED_RELEASE_MARKER") == MARKER
    assert "${{" not in step["run"]
    assert workflow["jobs"]["publish"].get("permissions") == {
        "contents": "write",
        "actions": "read",
    }
