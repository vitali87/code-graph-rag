import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

from codebase_rag import constants as cs

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
)
SHA = "a" * 40
NEWER = "b" * 40
OLDER = "c" * 40
PUSHED = "steps.commit.outputs.pushed == 'true'"


class Step(TypedDict, total=False):
    name: str
    id: str
    run: str
    env: dict[str, str]


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(name: str) -> Step:
    return next(
        s for s in _workflow()["jobs"]["bump-version"]["steps"] if s["name"] == name
    )


def _run(
    name: str, stubs: str, tmp_path: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("the Ubuntu workflow requires bash")
    output = tmp_path / "output"
    output.touch()
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-c", stubs + "\n" + _step(name)["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_SHA": SHA,
            "GITHUB_REPOSITORY": "vitali87/code-graph-rag",
            **env,
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=15,
    )
    lines = output.read_text(encoding=cs.ENCODING_UTF8).splitlines()
    return result, dict(line.split("=", 1) for line in lines if "=" in line)


def test_runs_are_queued_not_cancelled() -> None:
    workflow = _workflow()
    assert "concurrency" not in workflow
    concurrency = workflow["jobs"]["bump-version"]["concurrency"]
    assert concurrency["group"] == "version-bump-main"
    assert concurrency["cancel-in-progress"] is False


def _keeps_every_pending_run(concurrency: dict) -> bool:
    # By default a group holds one pending run and a newly queued run replaces
    # it. Admission order is not guaranteed, so a delayed older run can take
    # the slot from the newest one and then stand down at the tip check,
    # leaving the merge unbumped. `queue: max` keeps every run waiting; GitHub
    # rejects it alongside `cancel-in-progress: true`.
    return (
        concurrency.get("queue") == "max"
        and concurrency.get("cancel-in-progress") is False
    )


def test_a_pending_bump_run_is_never_replaced() -> None:
    assert _keeps_every_pending_run(_workflow()["jobs"]["bump-version"]["concurrency"])


@pytest.mark.parametrize(
    "concurrency",
    [
        {"group": "version-bump-main", "cancel-in-progress": False},
        {"group": "version-bump-main", "cancel-in-progress": False, "queue": "single"},
        {"group": "version-bump-main", "cancel-in-progress": True, "queue": "max"},
    ],
    ids=["default-slot", "single-slot", "cancels-in-progress"],
)
def test_a_concurrency_that_replaces_or_cancels_runs_is_rejected(
    concurrency: dict,
) -> None:
    assert not _keeps_every_pending_run(concurrency)


CHECK_STUBS = r"""
git() {
  case "$1" in
    ls-remote)
      [ "$LS_REMOTE_FAILS" = true ] && return 128
      printf '%s\trefs/heads/main\n' "$TIP"
      ;;
    show) printf 'version = "0.0.5"\n' ;;
  esac
}
"""


def test_stale_run_stands_down_before_bumping(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('version = "0.0.5"\n', encoding="utf-8")
    result, out = _run(
        "Check if version was manually changed", CHECK_STUBS, tmp_path, {"TIP": NEWER}
    )
    assert result.returncode == 0, result.stderr
    assert out["skip"] == "true"
    assert "standing down" in result.stdout


def test_newest_run_bumps(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('version = "0.0.5"\n', encoding="utf-8")
    result, out = _run(
        "Check if version was manually changed", CHECK_STUBS, tmp_path, {"TIP": SHA}
    )
    assert result.returncode == 0, result.stderr
    assert out["skip"] == "false"


COMMIT_STUBS = r"""
git() {
  case "$1" in
    push) printf 'git: push\n'; return "$PUSH_EXIT" ;;
    rev-parse) printf '%s\n' "$TIP" ;;
    merge-base) [ "$ANCESTOR" = true ] ;;
    *) printf 'git: %s\n' "$*" ;;
  esac
}
"""


def _commit(
    tmp_path: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    return _run(
        "Commit version bump",
        COMMIT_STUBS,
        tmp_path,
        {
            "NEW_VERSION": "0.1.0",
            "PUSH_EXIT": "0",
            "TIP": SHA,
            "ANCESTOR": "true",
            **env,
        },
    )


def test_successful_push_is_recorded(tmp_path: Path) -> None:
    result, out = _commit(tmp_path)
    assert result.returncode == 0, result.stderr
    assert out["pushed"] == "true"


def test_rejected_push_stands_down_when_main_moved_on(tmp_path: Path) -> None:
    result, out = _commit(tmp_path, PUSH_EXIT="1", TIP=NEWER)
    assert result.returncode == 0, result.stderr
    assert out["pushed"] == "false"
    assert "standing down" in result.stdout


@pytest.mark.parametrize("env", [{"TIP": SHA}, {"TIP": NEWER, "ANCESTOR": "false"}])
def test_rejected_push_fails_when_main_did_not_move_on(
    tmp_path: Path, env: dict[str, str]
) -> None:
    result, out = _commit(tmp_path, PUSH_EXIT="1", **env)
    assert result.returncode != 0
    assert "pushed" not in out


@pytest.mark.parametrize(
    "name",
    [
        "Require successful CI for the release source",
        "Create git tag",
        "Create release",
    ],
)
def test_nothing_is_tagged_or_released_without_a_pushed_bump(name: str) -> None:
    assert PUSHED in _step(name)["if"]


DECIDE_STUBS = r"""
gh() {
  local url=""
  for arg in "$@"; do
    case "$arg" in repos/*) url="$arg" ;; esac
  done
  case "$url" in
    */releases/latest) printf 'v0.0.1\n' ;;
    */compare/*)
      printf '%s\n' "$*" > compare_call
      [ "$COMPARE_FAILS" = true ] && return 1
      printf '%b' "$RANGE"
      ;;
    */pulls)
      [ "$PULLS_FAIL" = true ] && return 1
      cat "labels/$(basename "$(dirname "$url")")" 2> /dev/null || true
      ;;
    *) return 1 ;;
  esac
}
git() {
  case "$1" in
    log)
      case "${@: -1}" in -*) touch pwned ;; esac
      cat "messages/${@: -1}" 2> /dev/null
      ;;
    ls-remote)
      if [ "$2" = --exit-code ]; then
        return "$LS_REMOTE_EXIT"
      fi
      if [ "$3" = --refs ]; then
        [ "$TAG_LIST_FAILS" = true ] && return 128
        printf '%b' "$TAG_LIST"
      fi
      ;;
  esac
}
"""


def _commits(
    tmp_path: Path, messages: dict[str, str], labels: dict[str, str] | None = None
) -> None:
    for folder, entries in (("messages", messages), ("labels", labels or {})):
        (tmp_path / folder).mkdir()
        for sha, text in entries.items():
            (tmp_path / folder / sha).write_text(text + "\n", encoding="utf-8")


def _decide_run(
    tmp_path: Path, range_: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    return _run(
        "Decide whether this tag ships a release",
        DECIDE_STUBS,
        tmp_path,
        {
            "CURRENT": "0.0.1",
            "RANGE": range_,
            "RELEASE_EVERY": "50",
            "LS_REMOTE_EXIT": "0",
            "COMPARE_FAILS": "false",
            "PULLS_FAIL": "false",
            "TAG_LIST": "",
            "TAG_LIST_FAILS": "false",
            **env,
        },
    )


def _decide(tmp_path: Path, range_: str) -> dict[str, str]:
    result, out = _decide_run(tmp_path, range_)
    assert result.returncode == 0, result.stderr
    return out


def test_every_page_of_the_range_is_read(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    _decide(tmp_path, f"{SHA}\\n")
    call = (tmp_path / "compare_call").read_text(encoding="utf-8")
    assert "--paginate" in call.split()
    assert "per_page=100" in call


@pytest.mark.parametrize("range_", ["", "not-a-sha\\n"])
def test_unreadable_range_fails_instead_of_narrowing_the_scan(
    tmp_path: Path, range_: str
) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    result, out = _decide_run(tmp_path, range_)
    assert result.returncode != 0
    assert "refusing to decide the release" in result.stdout
    assert "release" not in out


def test_failed_compare_call_fails_the_run(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    result, out = _decide_run(tmp_path, f"{SHA}\\n", COMPARE_FAILS="true")
    assert result.returncode != 0
    assert "could not list the commits" in result.stdout
    assert "release" not in out


def test_without_any_release_tag_the_triggering_commit_is_scanned(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "fix: y [security]"})
    result, out = _decide_run(tmp_path, "", LS_REMOTE_EXIT="2")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "compare_call").exists()
    assert out["security"] == "true"


@pytest.mark.parametrize("status", ["1", "128"])
def test_failed_tag_lookup_fails_instead_of_narrowing_the_scan(
    tmp_path: Path, status: str
) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    result, out = _decide_run(tmp_path, f"{SHA}\\n", LS_REMOTE_EXIT=status)
    assert result.returncode != 0
    assert "could not look up tag" in result.stdout
    assert not (tmp_path / "compare_call").exists()
    assert "release" not in out


def test_security_fix_in_an_earlier_commit_of_the_range_ships(tmp_path: Path) -> None:
    _commits(tmp_path, {OLDER: "fix: patch [security]", SHA: "feat: x"})
    out = _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert out["security"] == "true"
    assert out["release"] == "true"


def test_security_label_on_an_earlier_commit_of_the_range_ships(tmp_path: Path) -> None:
    _commits(tmp_path, {OLDER: "fix: a", SHA: "feat: x"}, {OLDER: "security"})
    out = _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert out["security"] == "true"


def test_ordinary_range_does_not_ship(tmp_path: Path) -> None:
    _commits(tmp_path, {OLDER: "fix: a", SHA: "feat: x"})
    out = _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert out["security"] == "false"
    assert out["release"] == "false"


def test_non_sha_range_entries_never_reach_git(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    out = _decide(tmp_path, f"--output=pwned\\n{SHA}\\n")
    assert not (tmp_path / "pwned").exists()
    assert out["security"] == "false"


@pytest.mark.parametrize("tip", ["", "not-a-sha"])
def test_unreadable_tip_fails_instead_of_standing_down(
    tmp_path: Path, tip: str
) -> None:
    (tmp_path / "pyproject.toml").write_text('version = "0.0.5"\n', encoding="utf-8")
    result, out = _run(
        "Check if version was manually changed", CHECK_STUBS, tmp_path, {"TIP": tip}
    )
    assert result.returncode != 0
    assert "refusing to decide whether to bump" in result.stdout
    assert "skip" not in out


def test_failed_tip_lookup_fails_instead_of_standing_down(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('version = "0.0.5"\n', encoding="utf-8")
    result, out = _run(
        "Check if version was manually changed",
        CHECK_STUBS,
        tmp_path,
        {"TIP": SHA, "LS_REMOTE_FAILS": "true"},
    )
    assert result.returncode != 0
    assert "could not read the tip of main" in result.stdout
    assert "skip" not in out


def test_unreadable_commit_message_fails_the_run(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    result, out = _decide_run(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert result.returncode != 0
    assert f"could not read the message of {OLDER}" in result.stdout
    assert "release" not in out


def test_unreadable_pull_request_labels_fail_the_run(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    result, out = _decide_run(tmp_path, f"{SHA}\\n", PULLS_FAIL="true")
    assert result.returncode != 0
    assert "could not read the pull request labels" in result.stdout
    assert "release" not in out


def test_an_untagged_version_scans_from_the_newest_release_tag(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {OLDER: "fix: patch [security]", SHA: "chore: set version"})
    tags = (
        f"{OLDER}\\trefs/tags/v0.0.9\\n"
        f"{OLDER}\\trefs/tags/v0.1.10\\n"
        f"{OLDER}\\trefs/tags/v0.1.9\\n"
        f"{OLDER}\\trefs/tags/not-a-release\\n"
    )
    result, out = _decide_run(
        tmp_path, f"{OLDER}\\n{SHA}\\n", LS_REMOTE_EXIT="2", TAG_LIST=tags
    )
    assert result.returncode == 0, result.stderr
    call = (tmp_path / "compare_call").read_text(encoding="utf-8")
    assert f"compare/v0.1.10...{SHA}" in call
    assert out["security"] == "true"


def test_an_unlistable_tag_set_fails_instead_of_narrowing_the_scan(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    result, out = _decide_run(tmp_path, "", LS_REMOTE_EXIT="2", TAG_LIST_FAILS="true")
    assert result.returncode != 0
    assert "could not list release tags" in result.stdout
    assert "release" not in out
