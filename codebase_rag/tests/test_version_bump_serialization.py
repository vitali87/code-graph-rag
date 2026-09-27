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
    concurrency = _workflow()["concurrency"]
    assert concurrency["group"] == "version-bump-main"
    assert concurrency["cancel-in-progress"] is False


CHECK_STUBS = r"""
git() {
  case "$1" in
    ls-remote) printf '%s\trefs/heads/main\n' "$TIP" ;;
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
  case "$2" in
    */releases/latest) printf 'v0.0.1\n' ;;
    */compare/*) printf '%b' "$RANGE" ;;
    */pulls) printf '%s\n' "${LABELS[$(basename "$(dirname "$2")")]:-}" ;;
    *) return 1 ;;
  esac
}
git() {
  case "$1" in
    log) printf '%s\n' "${MESSAGES[${@: -1}]:-}" ;;
    ls-remote) printf '' ;;
  esac
}
"""


def _decide(tmp_path: Path, declare: str, range_: str) -> dict[str, str]:
    stubs = declare + DECIDE_STUBS
    result, out = _run(
        "Decide whether this tag ships a release",
        stubs,
        tmp_path,
        {"CURRENT": "0.0.1", "RANGE": range_, "RELEASE_EVERY": "50"},
    )
    assert result.returncode == 0, result.stderr
    return out


def test_security_fix_in_an_earlier_commit_of_the_range_ships(tmp_path: Path) -> None:
    declare = (
        f"declare -A MESSAGES=([{OLDER}]='fix: patch [security]' [{SHA}]='feat: x')\n"
        "declare -A LABELS=()\n"
    )
    out = _decide(tmp_path, declare, f"{OLDER}\\n{SHA}\\n")
    assert out["security"] == "true"
    assert out["release"] == "true"


def test_security_label_on_an_earlier_commit_of_the_range_ships(tmp_path: Path) -> None:
    declare = (
        f"declare -A MESSAGES=([{OLDER}]='fix: a' [{SHA}]='feat: x')\n"
        f"declare -A LABELS=([{OLDER}]=security)\n"
    )
    out = _decide(tmp_path, declare, f"{OLDER}\\n{SHA}\\n")
    assert out["security"] == "true"


def test_ordinary_range_does_not_ship(tmp_path: Path) -> None:
    declare = (
        f"declare -A MESSAGES=([{OLDER}]='fix: a' [{SHA}]='feat: x')\n"
        "declare -A LABELS=()\n"
    )
    out = _decide(tmp_path, declare, f"{OLDER}\\n{SHA}\\n")
    assert out["security"] == "false"
    assert out["release"] == "false"


def test_non_sha_range_entries_never_reach_git(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    declare = f"declare -A MESSAGES=([{SHA}]='feat: x')\ndeclare -A LABELS=()\n"
    stubs = declare + DECIDE_STUBS.replace(
        "log) printf", f'log) case "${{@: -1}}" in -*) touch {marker};; esac; printf'
    )
    result, out = _run(
        "Decide whether this tag ships a release",
        stubs,
        tmp_path,
        {
            "CURRENT": "0.0.1",
            "RANGE": f"--output={marker}\\n{SHA}\\n",
            "RELEASE_EVERY": "50",
        },
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert out["security"] == "false"
