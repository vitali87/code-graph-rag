import json
import os
import re
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
TAG_OBJECT = "d" * 40
# The workflow runs on Ubuntu, where jq ends lines with LF. A native jq.exe
# ends them with CRLF, and Git Bash keeps the CR inside the last word of each
# line, so `for SHA in $COMMITS` would build `commits/<sha>\r/pulls`. Strip it
# on Windows, keeping jq's own exit status for the `if ! X=$(jq ...)` checks.
CRLF_SAFE_JQ = (
    'jq() { command jq "$@" | tr -d \'\\r\'; return "${PIPESTATUS[0]}"; }\n'
    if os.name == "nt"
    else ""
)


class Step(TypedDict, total=False):
    name: str
    id: str
    run: str
    env: dict[str, str]


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


MARKER = _workflow()["env"]["DEFERRED_RELEASE_MARKER"]


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
    # Run from a file, not `bash -c`: Git Bash on Windows cuts a `-c` script
    # off at 8192 characters, and the stubs plus the "Decide" step outgrew
    # that, so bash saw a truncated script and failed to parse it. LF only,
    # as on the Ubuntu runner: a CR would stay inside each line's last word.
    script = tmp_path / "step.sh"
    script.write_text(
        CRLF_SAFE_JQ + stubs + "\n" + _step(name)["run"],
        encoding=cs.ENCODING_UTF8,
        newline="\n",
    )
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "./step.sh"],
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
      case "$*" in
        *refs/tags/*)
          printf '%s\n' "${@: -1}" >> tag_lookups
          return "${TAG_EXIT:-2}"
          ;;
      esac
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
    assert out["hand_set"] == "false"
    assert not (tmp_path / "tag_lookups").exists()


def _check_hand_set(
    tmp_path: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    # HEAD^ held 0.0.5, and the merge itself set 0.1.0.
    (tmp_path / "pyproject.toml").write_text('version = "0.1.0"\n', encoding="utf-8")
    return _run(
        "Check if version was manually changed",
        CHECK_STUBS,
        tmp_path,
        {"TIP": SHA, **env},
    )


def test_a_version_set_by_hand_is_tagged_without_a_bump(tmp_path: Path) -> None:
    result, out = _check_hand_set(tmp_path)
    assert result.returncode == 0, result.stderr
    assert out["skip"] == "false"
    assert out["hand_set"] == "true"
    assert _lines(tmp_path / "tag_lookups") == ["refs/tags/v0.1.0"]


def test_a_version_set_by_hand_that_is_already_tagged_stands_down(
    tmp_path: Path,
) -> None:
    result, out = _check_hand_set(tmp_path, TAG_EXIT="0")
    assert result.returncode == 0, result.stderr
    assert out["skip"] == "true"
    assert "nothing to tag" in result.stdout


def test_a_failed_tag_lookup_for_a_hand_set_version_fails_the_run(
    tmp_path: Path,
) -> None:
    result, out = _check_hand_set(tmp_path, TAG_EXIT="128")
    assert result.returncode != 0
    assert "could not look up tag v0.1.0" in result.stdout
    assert "skip" not in out


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


def test_a_version_set_by_hand_still_gets_its_bump_commit(tmp_path: Path) -> None:
    # Nothing may be left to change, but a later breaking scan stops there.
    result, _ = _commit(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "git: commit --allow-empty -m chore: bump version to 0.1.0" in (
        result.stdout
    )


def test_only_the_bump_commit_this_job_pushes_skips_the_job(tmp_path: Path) -> None:
    # A merge whose pull request is titled "chore: bump version to X" has the
    # phrase in its message too, and is the merge that sets a version by hand:
    # matching the phrase alone left that version untagged and unreleased.
    result, _ = _commit(tmp_path)
    assert result.returncode == 0, result.stderr
    author = re.search(r"^git: config user\.email (\S+)$", result.stdout, re.M)
    assert author is not None
    assert "git: commit --allow-empty -m chore: bump version to 0.1.0" in (
        result.stdout
    )
    assert _workflow()["jobs"]["bump-version"]["if"] == (
        "github.ref == 'refs/heads/main' && "
        "!(startsWith(github.event.head_commit.message, 'chore: bump version to ') && "
        f"github.event.head_commit.author.email == '{author[1]}')"
    )


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
  printf '%s\n' "$url" >> gh_calls
  case "$url" in
    */releases/latest) printf 'v0.0.1\n' ;;
    */compare/*)
      case " $* " in
        *" .status "*)
          printf '%s\n' "$url" >> status_calls
          [ "$STATUS_FAILS" = true ] && return 1
          tag="${url##*/compare/}"
          cat "status/${tag%%...*}" 2> /dev/null || printf 'ahead\n'
          return 0
          ;;
      esac
      printf '%s\n' "$*" > compare_call
      [ "$COMPARE_FAILS" = true ] && return 1
      cat compare_commits
      ;;
    */pulls)
      basename "$(dirname "$url")" >> pulls_calls
      [ "$PULLS_FAIL" = true ] && return 1
      cat "labels/$(basename "$(dirname "$url")")" 2> /dev/null || true
      ;;
    */git/tags/*)
      basename "$url" >> annotation_calls
      cat "annotations/$(basename "$url")" 2> /dev/null
      ;;
    */contents/pyproject.toml\?ref=*)
      printf '%s\n' "${url##*ref=}" >> contents_calls
      [ "$CONTENTS_FAIL" = true ] && return 1
      cat "versions/${url##*ref=}" 2> /dev/null
      ;;
    */issues/*/events*)
      pr="${url#*/issues/}"
      printf '%s\n' "${pr%%/*}" >> events_calls
      [ "$EVENTS_FAIL" = true ] && return 1
      cat "labelers/${pr%%/*}" 2> /dev/null
      ;;
    */collaborators/*/permission)
      login="${url#*/collaborators/}"
      printf '%s\n' "${login%%/*}" >> role_calls
      [ "$ROLE_FAIL" = true ] && return 1
      cat "roles/${login%%/*}" 2> /dev/null
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
      if [ "$2" = --tags ] && [ "$3" = origin ]; then
        [ "$TAG_LIST_FAILS" = true ] && return 128
        printf '%b' "$TAG_LIST"
      fi
      ;;
  esac
}
"""


def _commits(
    tmp_path: Path,
    messages: dict[str, str],
    labels: dict[str, str] | None = None,
    parents: dict[str, str] | None = None,
) -> None:
    for folder, entries in (
        ("messages", messages),
        ("labels", labels or {}),
        ("parents", parents or {}),
    ):
        (tmp_path / folder).mkdir()
        for sha, text in entries.items():
            # LF, as `gh` prints on the Ubuntu runner: awk and `read` would
            # keep a Windows CR inside the last field of each line.
            (tmp_path / folder / sha).write_text(
                text + "\n", encoding="utf-8", newline="\n"
            )


def _read_or(path: Path, default: str) -> str:
    if not path.is_file():
        return default
    return path.read_text(encoding="utf-8").rstrip("\n")


def _compare_commits(tmp_path: Path, range_: str) -> str:
    """The compare API's commits as the step's `--jq` prints them, a JSON line each.

    Built here, not by the `gh` stub: a `cat` and a `jq` per commit cost
    several process starts each, which Git Bash on Windows makes slow enough
    that a 52-commit range ran past the step's timeout. A commit without a
    `parents/` entry has the one listed before it as its parent.
    """
    lines = []
    prev = ""
    for sha in range_.replace("\\n", "\n").split():
        parent = _read_or(tmp_path / "parents" / sha, prev)
        commit = {
            "sha": sha,
            "parent": parent or None,
            "message": _read_or(tmp_path / "messages" / sha, ""),
        }
        lines.append(json.dumps(commit, ensure_ascii=False, separators=(",", ":")))
        prev = sha
    return "".join(f"{line}\n" for line in lines)


def _decide_run(
    tmp_path: Path, range_: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    if shutil.which("jq") is None:
        pytest.skip("the Ubuntu workflow requires jq")
    (tmp_path / "compare_commits").write_text(
        _compare_commits(tmp_path, range_), encoding="utf-8", newline="\n"
    )
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
            "STATUS_FAILS": "false",
            "CONTENTS_FAIL": "false",
            "EVENTS_FAIL": "false",
            "ROLE_FAIL": "false",
            "DEFERRED_RELEASE_MARKER": MARKER,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            **env,
        },
    )


def _decide(tmp_path: Path, range_: str) -> dict[str, str]:
    result, out = _decide_run(tmp_path, range_)
    assert result.returncode == 0, result.stderr
    return out


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").split() if path.exists() else []


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
    assert out["breaking"] == "false"


def test_breaking_label_on_an_earlier_commit_of_the_range_ships(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {OLDER: "feat: a", SHA: "fix: b"}, {OLDER: "breaking change"})
    out = _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert out["breaking"] == "true"
    # A breaking change does not wait for the cadence, like a security fix.
    assert out["release"] == "true"


def test_a_security_fix_does_not_hide_a_breaking_label(tmp_path: Path) -> None:
    # The security marker is known before any label is read; the scan must
    # still reach the earlier commit's pull request.
    _commits(
        tmp_path,
        {OLDER: "feat: a", SHA: "fix: b [security]"},
        {OLDER: "breaking change"},
    )
    out = _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert out["security"] == "true"
    assert out["breaking"] == "true"


def test_only_the_exact_breaking_change_label_counts(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"}, {SHA: "breaking\nbreaking changes"})
    assert _decide(tmp_path, f"{SHA}\\n")["breaking"] == "false"


def test_the_pull_requests_labelled_breaking_are_named(tmp_path: Path) -> None:
    # The release names a labelled pull request that added no notes, so the
    # numbers are passed on, not only the flag (issue #3431). A near-miss
    # label and a number that is not one name nothing.
    _commits(
        tmp_path,
        {OLDER: "feat: a", SHA: "fix: b"},
        {
            OLDER: "#pull-request\n#41\nbreaking change",
            SHA: "#pull-request\n#42\nbreaking changes\n"
            "#pull-request\n#1/../x\nbreaking change",
        },
    )
    assert _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")["breaking_prs"] == "41"


def _security_release_with_two_breaking_changes(tmp_path: Path) -> dict[str, str]:
    # A security release ships whatever else the range holds, so its bump is
    # settled by the first breaking label; the release section still names
    # the labelled pull request behind it (issue #3431).
    _commits(
        tmp_path,
        {OLDER: "feat: a", SHA: "fix: b [security]"},
        {
            OLDER: f"{RELEASE_NOW_PR}\nbreaking change",
            SHA: "#pull-request\n#41\nbreaking change",
        },
    )
    return _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")


def test_a_security_release_names_every_breaking_pull_request(tmp_path: Path) -> None:
    out = _security_release_with_two_breaking_changes(tmp_path)
    assert out["security"] == "true"
    assert out["breaking_prs"] == "12 41"


def test_a_settled_security_release_reads_only_the_labels(tmp_path: Path) -> None:
    # Past that point the "release now" lookups would change nothing and cost
    # calls that can fail the run.
    _security_release_with_two_breaking_changes(tmp_path)
    assert _lines(tmp_path / "pulls_calls") == [SHA, OLDER]
    assert not (tmp_path / "events_calls").exists()


RELEASE_NOW_PR = "#pull-request\n#12\nrelease now"


def _labelled_by(
    tmp_path: Path, labelers: dict[str, str], roles: dict[str, str]
) -> None:
    # What the events query prints per pull request ("<type> <login>" for each
    # time "release now" was applied, oldest first), and each login's role.
    for folder, entries in (("labelers", labelers), ("roles", roles)):
        (tmp_path / folder).mkdir()
        for name, text in entries.items():
            (tmp_path / folder / name).write_text(
                text + "\n", encoding="utf-8", newline="\n"
            )


@pytest.mark.parametrize("role", ["admin", "maintain"])
def test_release_now_label_from_a_maintainer_ships(tmp_path: Path, role: str) -> None:
    _commits(tmp_path, {OLDER: "fix: a", SHA: "feat: x"}, {OLDER: RELEASE_NOW_PR})
    _labelled_by(tmp_path, {"12": "User vitali87"}, {"vitali87": role})
    out = _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert out["release"] == "true"
    # No security notice and no breaking bump: the label only skips the wait.
    assert out["security"] == "false"
    assert out["breaking"] == "false"
    assert _lines(tmp_path / "events_calls") == ["12"]


@pytest.mark.parametrize("role", ["write", "triage", "read", ""])
def test_release_now_label_from_anyone_else_is_ignored(
    tmp_path: Path, role: str
) -> None:
    _commits(tmp_path, {SHA: "feat: x"}, {SHA: RELEASE_NOW_PR})
    _labelled_by(tmp_path, {"12": "User someone"}, {"someone": role})
    assert _decide(tmp_path, f"{SHA}\\n")["release"] == "false"
    assert "#12 ignored" in (tmp_path / "summary").read_text(encoding="utf-8")


def test_release_now_label_from_a_bot_is_ignored_without_a_role_lookup(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "feat: x"}, {SHA: RELEASE_NOW_PR})
    _labelled_by(tmp_path, {"12": "Bot claude[bot]"}, {})
    assert _decide(tmp_path, f"{SHA}\\n")["release"] == "false"
    assert not (tmp_path / "role_calls").exists()


def test_the_last_application_of_release_now_decides(tmp_path: Path) -> None:
    # Removed and applied again: the label in force is the latest one.
    _commits(tmp_path, {SHA: "feat: x"}, {SHA: RELEASE_NOW_PR})
    _labelled_by(
        tmp_path, {"12": "User vitali87\nBot claude[bot]"}, {"vitali87": "admin"}
    )
    assert _decide(tmp_path, f"{SHA}\\n")["release"] == "false"


def test_release_now_label_with_no_recorded_application_is_ignored(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "feat: x"}, {SHA: RELEASE_NOW_PR})
    _labelled_by(tmp_path, {"12": ""}, {})
    assert _decide(tmp_path, f"{SHA}\\n")["release"] == "false"


@pytest.mark.parametrize("failing", ["EVENTS_FAIL", "ROLE_FAIL"])
def test_an_unreadable_labeller_fails_instead_of_guessing(
    tmp_path: Path, failing: str
) -> None:
    _commits(tmp_path, {SHA: "feat: x"}, {SHA: RELEASE_NOW_PR})
    _labelled_by(tmp_path, {"12": "User vitali87"}, {"vitali87": "admin"})
    result, out = _decide_run(tmp_path, f"{SHA}\\n", **{failing: "true"})
    assert result.returncode != 0
    assert "release" not in out


def test_only_the_exact_release_now_label_counts(tmp_path: Path) -> None:
    _commits(
        tmp_path, {SHA: "feat: x"}, {SHA: "#pull-request\n#12\nrelease\nrelease nowx"}
    )
    _labelled_by(tmp_path, {"12": "User vitali87"}, {"vitali87": "admin"})
    assert _decide(tmp_path, f"{SHA}\\n")["release"] == "false"
    assert not (tmp_path / "events_calls").exists()


def test_a_pull_request_number_that_is_not_one_reaches_no_url(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"}, {SHA: "#pull-request\n#1/../x\nrelease now"})
    assert _decide(tmp_path, f"{SHA}\\n")["release"] == "false"
    assert not (tmp_path / "events_calls").exists()


def test_every_label_the_decide_step_reads_is_in_the_manifest() -> None:
    # The label sync creates only what labels.yml lists; a label read here but
    # missing there could never be applied, and its release would never ship.
    manifest = yaml.safe_load(
        (WORKFLOW.parents[1] / "labels.yml").read_text(encoding="utf-8")
    )
    names = {label["name"] for label in manifest}
    script = _step("Decide whether this tag ships a release")["run"]
    for label in ("security", "breaking change", "release now"):
        assert label in script
        assert label in names


def test_the_labels_are_read_before_the_bump_type_is_chosen() -> None:
    names = [s["name"] for s in _workflow()["jobs"]["bump-version"]["steps"]]
    assert names.index("Decide whether this tag ships a release") < names.index(
        "Determine bump type"
    )
    env = _step("Determine bump type")["env"]
    assert env["BREAKING"] == "${{ steps.decide.outputs.breaking }}"
    assert env["HAND_SET"] == "${{ steps.check_manual.outputs.hand_set }}"


@pytest.mark.parametrize(
    ("event", "breaking", "dispatched", "expected"),
    [
        ("push", "false", "", "patch"),
        ("push", "true", "", "breaking"),
        # A dispatched type is an explicit choice and overrides the labels.
        ("workflow_dispatch", "true", "patch", "patch"),
        ("workflow_dispatch", "false", "minor", "minor"),
    ],
)
def test_the_bump_type_follows_the_breaking_label_unless_dispatched(
    tmp_path: Path, event: str, breaking: str, dispatched: str, expected: str
) -> None:
    result, out = _run(
        "Determine bump type",
        "",
        tmp_path,
        {
            "GITHUB_EVENT_NAME": event,
            "BREAKING": breaking,
            "DISPATCH_BUMP_TYPE": dispatched,
        },
    )
    assert result.returncode == 0, result.stderr
    assert out["type"] == expected


@pytest.mark.parametrize(
    ("event", "breaking", "dispatched"),
    [("push", "true", ""), ("workflow_dispatch", "false", "minor")],
)
def test_a_version_set_by_hand_is_not_bumped_again(
    tmp_path: Path, event: str, breaking: str, dispatched: str
) -> None:
    result, out = _run(
        "Determine bump type",
        "",
        tmp_path,
        {
            "GITHUB_EVENT_NAME": event,
            "BREAKING": breaking,
            "DISPATCH_BUMP_TYPE": dispatched,
            "HAND_SET": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    assert out["type"] == "none"


def test_non_sha_range_entries_never_reach_git_or_a_url(tmp_path: Path) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    out = _decide(tmp_path, f"--output=pwned\\n{SHA}\\n")
    assert not (tmp_path / "pwned").exists()
    assert _lines(tmp_path / "pulls_calls") == [SHA]
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
    _commits(tmp_path, {})
    result, out = _decide_run(tmp_path, "", LS_REMOTE_EXIT="2")
    assert result.returncode != 0
    assert f"could not read the message of {SHA}" in result.stdout
    assert "release" not in out


BRANCH_COMMITS = [f"{n:040x}" for n in range(1, 51)]
OUTSIDE = "e" * 40


def test_label_lookups_follow_mains_first_parent_line_only(tmp_path: Path) -> None:
    _commits(
        tmp_path,
        {
            OLDER: "fix: a",
            SHA: "Merge pull request #2",
            **dict.fromkeys(BRANCH_COMMITS, "wip"),
        },
        parents={OLDER: OUTSIDE, SHA: OLDER, **dict.fromkeys(BRANCH_COMMITS, OUTSIDE)},
    )
    range_ = "\\n".join([OLDER, *BRANCH_COMMITS, SHA]) + "\\n"
    out = _decide(tmp_path, range_)
    assert out["security"] == "false"
    assert _lines(tmp_path / "pulls_calls") == [SHA, OLDER]
    assert len(_lines(tmp_path / "gh_calls")) == 4


def test_security_marker_on_a_branch_commit_ships_reading_main_labels_only(
    tmp_path: Path,
) -> None:
    # The labels still decide the bump, so a security release reads them too,
    # but only along main's first-parent line, as an ordinary scan does.
    _commits(
        tmp_path,
        {
            OLDER: "fix: a",
            SHA: "Merge pull request #2",
            **dict.fromkeys(BRANCH_COMMITS, "wip"),
            BRANCH_COMMITS[7]: "fix: patch [security]",
        },
        parents={OLDER: OUTSIDE, SHA: OLDER, **dict.fromkeys(BRANCH_COMMITS, OUTSIDE)},
    )
    range_ = "\\n".join([OLDER, *BRANCH_COMMITS, SHA]) + "\\n"
    out = _decide(tmp_path, range_)
    assert out["security"] == "true"
    assert out["release"] == "true"
    assert _lines(tmp_path / "pulls_calls") == [SHA, OLDER]
    assert len(_lines(tmp_path / "gh_calls")) == 4


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
    _versions(tmp_path, {OLDER: "0.0.0"})
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


def test_an_untagged_version_skips_a_newer_tag_main_never_reached(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {OLDER: "fix: patch [security]", SHA: "chore: set version"})
    _versions(tmp_path, {OLDER: "0.0.0"})
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "v0.2.0").write_text("behind\n", encoding="utf-8")
    (tmp_path / "status" / "v0.1.11").write_text("diverged\n", encoding="utf-8")
    tags = (
        f"{OLDER}\\trefs/tags/v0.1.10\\n"
        f"{OLDER}\\trefs/tags/v0.2.0\\n"
        f"{OLDER}\\trefs/tags/v0.1.11\\n"
    )
    result, out = _decide_run(
        tmp_path, f"{OLDER}\\n{SHA}\\n", LS_REMOTE_EXIT="2", TAG_LIST=tags
    )
    assert result.returncode == 0, result.stderr
    checked = (tmp_path / "status_calls").read_text(encoding="utf-8").split()
    assert [url.split("/compare/")[1].split("...")[0] for url in checked] == [
        "v0.2.0",
        "v0.1.11",
        "v0.1.10",
    ]
    call = (tmp_path / "compare_call").read_text(encoding="utf-8")
    assert f"compare/v0.1.10...{SHA}" in call
    assert out["security"] == "true"


def test_an_untagged_version_with_no_tag_behind_it_scans_the_triggering_commit(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "fix: y [security]"})
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "v0.2.0").write_text("behind\n", encoding="utf-8")
    result, out = _decide_run(
        tmp_path,
        "",
        LS_REMOTE_EXIT="2",
        TAG_LIST=f"{OLDER}\\trefs/tags/v0.2.0\\n",
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "compare_call").exists()
    assert out["security"] == "true"


def test_a_failed_ancestry_check_fails_instead_of_narrowing_the_scan(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    result, out = _decide_run(
        tmp_path,
        f"{SHA}\\n",
        LS_REMOTE_EXIT="2",
        TAG_LIST=f"{OLDER}\\trefs/tags/v0.1.0\\n",
        STATUS_FAILS="true",
    )
    assert result.returncode != 0
    assert "could not compare v0.1.0" in result.stdout
    assert not (tmp_path / "compare_call").exists()
    assert "release" not in out


def _annotated(tmp_path: Path, tag: str, message: str, obj: str) -> str:
    annotations = tmp_path / "annotations"
    annotations.mkdir(exist_ok=True)
    (annotations / obj).write_text(message + "\n", encoding="utf-8")
    return f"{obj}\\trefs/tags/{tag}\\n{NEWER}\\trefs/tags/{tag}" + "^{}\\n"


def test_a_deferred_release_scans_its_commits_again(tmp_path: Path) -> None:
    """A security fix whose release was deferred still ships on a later bump,
    however long the streak of deferrals: their tags are never where the scan
    starts, so the fix's commit is read again."""
    _commits(tmp_path, {OLDER: "fix: patch [security]", SHA: "feat: x"})
    deferred = f"{MARKER}: CI is not proven for {OLDER}"
    tags = (
        f"{OLDER}\\trefs/tags/v0.1.11\\n"
        + _annotated(tmp_path, "v0.1.12", deferred, "1" * 40)
        + _annotated(tmp_path, "v0.1.13", deferred, "2" * 40)
    )
    result, out = _decide_run(
        tmp_path, f"{OLDER}\\n{SHA}\\n", CURRENT="0.1.13", TAG_LIST=tags
    )
    assert result.returncode == 0, result.stderr
    assert _lines(tmp_path / "status_calls") == [
        f"repos/vitali87/code-graph-rag/compare/v0.1.11...{SHA}?per_page=1"
    ]
    call = (tmp_path / "compare_call").read_text(encoding="utf-8")
    assert f"compare/v0.1.11...{SHA}" in call
    assert out["security"] == "true"
    assert out["release"] == "true"


def test_an_annotated_tag_that_deferred_nothing_still_bounds_the_scan(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    tags = f"{OLDER}\\trefs/tags/v0.1.11\\n" + _annotated(
        tmp_path, "v0.1.12", "v0.1.12", TAG_OBJECT
    )
    result, out = _decide_run(tmp_path, f"{SHA}\\n", CURRENT="0.1.12", TAG_LIST=tags)
    assert result.returncode == 0, result.stderr
    assert _lines(tmp_path / "annotation_calls") == [TAG_OBJECT]
    call = (tmp_path / "compare_call").read_text(encoding="utf-8")
    assert f"compare/v0.1.12...{SHA}" in call
    assert out["release"] == "false"


def test_annotations_up_to_the_latest_release_are_not_read(tmp_path: Path) -> None:
    """The latest release resolved every deferral before it, and reading them
    would make each run's cost grow with the repository's age."""
    _commits(tmp_path, {SHA: "feat: x"})
    tags = _annotated(tmp_path, "v0.0.0", f"{MARKER}: old", "1" * 40) + _annotated(
        tmp_path, "v0.0.1", f"{MARKER}: old", "2" * 40
    )
    result, out = _decide_run(tmp_path, f"{SHA}\\n", TAG_LIST=tags)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "annotation_calls").exists()
    call = (tmp_path / "compare_call").read_text(encoding="utf-8")
    assert f"compare/v0.0.1...{SHA}" in call


def test_an_unreadable_annotation_fails_instead_of_narrowing_the_scan(
    tmp_path: Path,
) -> None:
    _commits(tmp_path, {SHA: "feat: x"})
    tags = f"{TAG_OBJECT}\\trefs/tags/v0.1.12\\n{NEWER}\\trefs/tags/v0.1.12" + "^{}\\n"
    result, out = _decide_run(tmp_path, f"{SHA}\\n", CURRENT="0.1.12", TAG_LIST=tags)
    assert result.returncode != 0
    assert "could not read the annotation of v0.1.12" in result.stdout
    assert not (tmp_path / "compare_call").exists()
    assert "release" not in out


# The commit a bump run pushes, and a merge that set the version by hand.
BUMP_COMMIT = "f" * 40
HAND_SET = "9" * 40
# What the label query prints for each pull request a commit belongs to.
PULL_REQUEST = "#pull-request"


def _versions(tmp_path: Path, versions: dict[str, str]) -> None:
    (tmp_path / "versions").mkdir()
    for sha, version in versions.items():
        (tmp_path / "versions" / sha).write_text(
            f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8"
        )


def _deferred_breaking_bump(tmp_path: Path, labels: dict[str, str]) -> dict[str, str]:
    # A breaking merge bumped 0.1.11 to 0.2.0, but its release was deferred,
    # so this run's range starts back at v0.1.11 and holds that merge again.
    _commits(
        tmp_path,
        {OLDER: "Merge pull request #1", BUMP_COMMIT: "chore: bump version to 0.2.0"}
        | {SHA: "Merge pull request #2"},
        labels,
    )
    tags = f"{OLDER}\\trefs/tags/v0.1.11\\n" + _annotated(
        tmp_path, "v0.2.0", f"{MARKER}: CI is not proven", "1" * 40
    )
    result, out = _decide_run(
        tmp_path,
        f"{OLDER}\\n{BUMP_COMMIT}\\n{SHA}\\n",
        CURRENT="0.2.0",
        TAG_LIST=tags,
    )
    assert result.returncode == 0, result.stderr
    assert f"compare/v0.1.11...{SHA}" in (tmp_path / "compare_call").read_text(
        encoding="utf-8"
    )
    return out


def test_a_breaking_change_behind_a_deferred_bump_is_not_counted_again(
    tmp_path: Path,
) -> None:
    out = _deferred_breaking_bump(tmp_path, {OLDER: "breaking change"})
    assert out["breaking"] == "false"


def test_a_deferred_breaking_release_ships_on_the_next_bump(tmp_path: Path) -> None:
    # Counted once for the number, but its release is still owed.
    out = _deferred_breaking_bump(tmp_path, {OLDER: "breaking change"})
    assert out["release"] == "true"


def test_the_security_scan_still_reads_past_the_last_bump(tmp_path: Path) -> None:
    out = _deferred_breaking_bump(tmp_path, {OLDER: "security\nbreaking change"})
    assert out["security"] == "true"
    assert out["breaking"] == "false"
    assert _lines(tmp_path / "pulls_calls") == [SHA, BUMP_COMMIT, OLDER]


def test_a_breaking_change_after_a_deferred_bump_counts(tmp_path: Path) -> None:
    out = _deferred_breaking_bump(tmp_path, {SHA: "breaking change"})
    assert out["breaking"] == "true"


def _hand_set_version(tmp_path: Path, labels: dict[str, str]) -> dict[str, str]:
    # HAND_SET changed the version from 0.1.89 to 0.2.0 itself, and its own
    # run never tagged it (one from before such runs did, or that failed), so
    # no tag names 0.2.0.
    _commits(
        tmp_path,
        {HAND_SET: "Merge pull request #1", SHA: "Merge pull request #2"},
        labels,
        parents={HAND_SET: OUTSIDE, SHA: HAND_SET},
    )
    _versions(tmp_path, {OUTSIDE: "0.1.89", HAND_SET: "0.2.0"})
    result, out = _decide_run(
        tmp_path,
        f"{HAND_SET}\\n{SHA}\\n",
        CURRENT="0.2.0",
        LS_REMOTE_EXIT="2",
        TAG_LIST=f"{OLDER}\\trefs/tags/v0.1.89\\n",
    )
    assert result.returncode == 0, result.stderr
    return out


def test_a_hand_set_version_already_accounts_for_its_breaking_change(
    tmp_path: Path,
) -> None:
    out = _hand_set_version(tmp_path, {HAND_SET: "breaking change"})
    assert out["breaking"] == "false"
    assert _lines(tmp_path / "contents_calls") == [HAND_SET, OUTSIDE]


def test_a_hand_set_breaking_change_still_ships(tmp_path: Path) -> None:
    # Its own run released nothing, so this later one does.
    out = _hand_set_version(tmp_path, {HAND_SET: "breaking change"})
    assert out["release"] == "true"


def test_a_breaking_change_ships_on_the_merge_that_sets_its_version(
    tmp_path: Path,
) -> None:
    # The run for the merge that set 0.2.0 itself, before anything tags it.
    _commits(
        tmp_path,
        {SHA: "Merge pull request #1"},
        {SHA: f"{PULL_REQUEST}\nbreaking change"},
        parents={SHA: OUTSIDE},
    )
    _versions(tmp_path, {OUTSIDE: "0.1.89"})
    result, out = _decide_run(
        tmp_path,
        f"{SHA}\\n",
        CURRENT="0.2.0",
        LS_REMOTE_EXIT="2",
        TAG_LIST=f"{OLDER}\\trefs/tags/v0.1.89\\n",
    )
    assert result.returncode == 0, result.stderr
    # The number chosen by hand already accounts for it.
    assert out["breaking"] == "false"
    assert out["release"] == "true"


def test_a_breaking_change_after_a_hand_set_version_counts(tmp_path: Path) -> None:
    out = _hand_set_version(tmp_path, {SHA: "breaking change"})
    assert out["breaking"] == "true"


def test_a_tagged_version_reads_no_earlier_version(tmp_path: Path) -> None:
    _commits(tmp_path, {OLDER: "fix: a", SHA: "feat: x"}, {OLDER: "breaking change"})
    out = _decide(tmp_path, f"{OLDER}\\n{SHA}\\n")
    assert out["breaking"] == "true"
    assert not (tmp_path / "contents_calls").exists()


def test_an_unreadable_earlier_version_fails_the_run(tmp_path: Path) -> None:
    _commits(
        tmp_path,
        {HAND_SET: "Merge pull request #1", SHA: "Merge pull request #2"},
        parents={HAND_SET: OUTSIDE, SHA: HAND_SET},
    )
    result, out = _decide_run(
        tmp_path,
        f"{HAND_SET}\\n{SHA}\\n",
        CURRENT="0.2.0",
        LS_REMOTE_EXIT="2",
        TAG_LIST=f"{OLDER}\\trefs/tags/v0.1.89\\n",
        CONTENTS_FAIL="true",
    )
    assert result.returncode != 0
    assert f"could not read the version at {HAND_SET}" in result.stdout
    assert "breaking" not in out


def test_the_breaking_scan_stops_at_the_commit_a_bump_makes() -> None:
    commit = _step("Commit version bump")["run"]
    decide = _step("Decide whether this tag ships a release")["run"]
    assert (
        'git commit --allow-empty -m "chore: bump version to ${NEW_VERSION}"' in commit
    )
    assert '"chore: bump version to "*)' in decide


def test_a_pull_request_titled_like_a_bump_is_no_boundary(tmp_path: Path) -> None:
    # A squash merge takes its pull request's title; only the bump step's
    # own commit, pushed straight to main, belongs to no pull request.
    _commits(
        tmp_path,
        {
            OLDER: "Merge pull request #1",
            NEWER: "chore: bump version to 9.9.9 (#2)",
            SHA: "Merge pull request #3",
        },
        {
            OLDER: f"{PULL_REQUEST}\nbreaking change",
            NEWER: PULL_REQUEST,
            SHA: PULL_REQUEST,
        },
    )
    out = _decide(tmp_path, f"{OLDER}\\n{NEWER}\\n{SHA}\\n")
    assert out["breaking"] == "true"


def _pulls_query() -> str:
    decide = _step("Decide whether this tag ships a release")["run"]
    match = re.search(r"/pulls\" \\\n\s*--jq '([^']*)'", decide)
    assert match, "the scan reads labels with one --jq query"
    return match.group(1)


@pytest.mark.parametrize(
    ("pulls", "expected"),
    [
        # The bump step's own commit: no pull request at all.
        ([], []),
        (
            [
                {
                    "number": 7,
                    "labels": [{"name": "security"}, {"name": "breaking change"}],
                }
            ],
            [PULL_REQUEST, "#7", "security", "breaking change"],
        ),
        ([{"number": 8, "labels": []}], [PULL_REQUEST, "#8"]),
    ],
)
def test_the_label_query_marks_each_pull_request(
    pulls: list[dict[str, int | list[dict[str, str]]]], expected: list[str]
) -> None:
    if shutil.which("jq") is None:
        pytest.skip("the Ubuntu workflow requires jq")
    result = subprocess.run(
        ["jq", "-r", _pulls_query()],
        input=json.dumps(pulls),
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=True,
    )
    assert result.stdout.splitlines() == expected
