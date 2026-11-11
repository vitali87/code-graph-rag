import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "version-bump-supersede.yml"
)
BUMP_WORKFLOW = WORKFLOW.with_name("version-bump.yml")
OURS = "a" * 40
OLDER = "b" * 40
OLDEST = "c" * 40
NEWER = "d" * 40

STUBS = r"""
gh() {
  local url="" post=false
  for arg in "$@"; do
    case "$arg" in
      repos/*) url="$arg" ;;
      POST) post=true ;;
    esac
  done
  case "$url" in
    */actions/workflows/*/runs\?*)
      printf '%s\n' "$url" >> list_calls
      status="${url##*status=}"
      status="${status%%&*}"
      case " $LIST_FAILS " in *" $status "*) return 1 ;; esac
      cat "runs/$status" 2> /dev/null
      ;;
    */compare/*)
      base="${url##*/compare/}"
      base="${base%%...*}"
      printf '%s\n' "$base" >> compare_calls
      cat "ahead/$base" 2> /dev/null || return 1
      ;;
    */actions/runs/*/cancel)
      [ "$post" = true ] || return 1
      id="${url%/cancel}"
      id="${id##*/}"
      case " $CANCEL_FAILS " in *" $id "*) return 1 ;; esac
      printf '%s\n' "$id" >> cancelled
      ;;
    *) return 1 ;;
  esac
}
"""


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    return _workflow()["jobs"]["supersede"]


def _run(
    tmp_path: Path,
    runs: dict[str, list[tuple[str, str]]],
    ahead: dict[str, str],
    **env: str,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("the Ubuntu workflow requires bash")
    for folder, entries in (
        ("runs", {k: "".join(f"{i} {s}\n" for i, s in v) for k, v in runs.items()}),
        ("ahead", {k: v + "\n" for k, v in ahead.items()}),
    ):
        (tmp_path / folder).mkdir()
        for name, text in entries.items():
            # LF, as `gh` prints on the Ubuntu runner: `read` would keep a
            # Windows CR inside the SHA and the run would never match.
            (tmp_path / folder / name).write_text(text, encoding="utf-8", newline="\n")
    script = tmp_path / "step.sh"
    script.write_text(
        STUBS + "\n" + _job()["steps"][0]["run"],
        encoding=cs.ENCODING_UTF8,
        newline="\n",
    )
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "./step.sh"],
        cwd=tmp_path,
        env={
            **os.environ,
            "GITHUB_SHA": OURS,
            "GITHUB_REPOSITORY": "vitali87/code-graph-rag",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "LIST_FAILS": "",
            "CANCEL_FAILS": "",
            **env,
        },
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=15,
    )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def _summary(tmp_path: Path) -> str:
    return (tmp_path / "summary").read_text(encoding="utf-8")


def test_waiting_runs_for_contained_commits_are_cancelled(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"pending": [("11", OLDER)], "queued": [("10", OLDEST)]},
        {OLDER: "ahead", OLDEST: "ahead"},
    )
    assert result.returncode == 0, result.stderr
    assert sorted(_lines(tmp_path / "cancelled")) == ["10", "11"]
    assert "2 cancelled" in _summary(tmp_path)


@pytest.mark.parametrize("status", ["identical", "behind", "diverged", ""])
def test_a_run_for_a_commit_this_one_does_not_contain_is_kept(
    tmp_path: Path, status: str
) -> None:
    # "behind" is a newer merge, whose run is the one that has to bump;
    # "identical" is this merge's own run.
    result = _run(tmp_path, {"pending": [("12", NEWER)]}, {NEWER: status})
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "cancelled").exists()


def test_a_failed_compare_keeps_the_run(tmp_path: Path) -> None:
    result = _run(tmp_path, {"pending": [("12", NEWER)]}, {})
    assert result.returncode == 0, result.stderr
    assert _lines(tmp_path / "compare_calls") == [NEWER]
    assert not (tmp_path / "cancelled").exists()


def test_only_runs_not_yet_started_are_listed(tmp_path: Path) -> None:
    # A started run may have pushed its bump and be waiting on CI to tag it.
    _run(tmp_path, {}, {})
    calls = _lines(tmp_path / "list_calls")
    statuses = sorted(c.split("status=")[1].split("&")[0] for c in calls)
    assert statuses == ["pending", "queued", "requested", "waiting"]
    for call in calls:
        assert "/actions/workflows/version-bump.yml/runs?" in call
        assert "branch=main" in call
        assert "event=push" in call


def test_a_run_listed_twice_is_cancelled_once(tmp_path: Path) -> None:
    # A run admitted between two listings shows up as pending and as queued.
    result = _run(
        tmp_path,
        {"pending": [("11", OLDER)], "queued": [("11", OLDER)]},
        {OLDER: "ahead"},
    )
    assert result.returncode == 0, result.stderr
    assert _lines(tmp_path / "cancelled") == ["11"]


def test_a_failed_listing_or_cancel_warns_and_carries_on(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"queued": [("10", OLDEST), ("11", OLDER)]},
        {OLDER: "ahead", OLDEST: "ahead"},
        LIST_FAILS="pending",
        CANCEL_FAILS="10",
    )
    assert result.returncode == 0, result.stderr
    assert "could not list the pending bump runs" in result.stdout
    assert "could not cancel run 10" in result.stdout
    assert _lines(tmp_path / "cancelled") == ["11"]
    assert "1 cancelled" in _summary(tmp_path)


def test_entries_that_are_not_a_run_id_and_sha_reach_no_url(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"pending": [("1/../x", OLDER), ("11", "../" + OLDER)]},
        {OLDER: "ahead"},
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "compare_calls").exists()
    assert not (tmp_path / "cancelled").exists()


def test_it_runs_outside_the_bump_queue_on_every_merge_to_main() -> None:
    workflow = _workflow()
    on = workflow.get("on", workflow.get(True))
    assert on == {"push": {"branches": ["main"]}}
    assert "concurrency" not in workflow
    job = _job()
    assert "concurrency" not in job
    assert job["permissions"] == {"actions": "write", "contents": "read"}
    # The bump commit's own push supersedes nothing: its merge's run bumped.
    # The same commits skip both workflows, so a merge titled like the bump
    # commit, which version-bump.yml runs for, still cancels what it supersedes.
    bump = yaml.safe_load(BUMP_WORKFLOW.read_text(encoding="utf-8"))
    assert job["if"].startswith(
        "!(startsWith(github.event.head_commit.message, 'chore: bump version to ') && "
    )
    assert bump["jobs"]["bump-version"]["if"].endswith(" && " + job["if"])
