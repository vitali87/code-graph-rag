"""The Greptile gate must reach its deadline branch even when the API stalls.

The gate polls GitHub for a review of the head commit and fails after a
deadline, printing the remedy: request a review, then re-run the job. That
message is the whole point of failing fast (issue #1507) -- a contributor who
gets a bare job timeout instead has to rediscover the handle and the rerun
command themselves.

The deadline is only consulted AFTER the API call returns. An unbounded `gh
api` can therefore block past the deadline and into GitHub's job timeout, which
kills the job without running the failure branch at all. Capping the poll at 20
minutes made this MORE reachable rather than less: the job timeout is now 25
minutes, so an API stall of a few minutes is enough.

Found by Greptile on #1510 with an executed reproducer.

The tests EXECUTE the gate's own shell, extracted from the workflow, against a
stubbed API. A structural check for the word `timeout` would pass on a
`timeout` that bounds the wrong command or whose non-zero exit aborts the loop
under `set -e`, and neither would emit the message this guards.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

JOB_ID = "greptile-gate"


def _gate_script() -> str:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"][JOB_ID]["steps"]
    return "\n".join(step.get("run", "") for step in steps)


def _run_gate(
    tmp_path: Path, *, gh_behaviour: str, deadline_seconds: int, wall_clock: float
) -> subprocess.CompletedProcess[str]:
    """Execute the gate script with `gh` and `date` stubbed.

    `deadline_seconds` shrinks the gate's own budget so the test does not wait
    20 real minutes; `wall_clock` bounds the whole run so a hang is a FAILURE
    with output rather than a suite that never finishes.
    """
    script = _gate_script()
    # The gate computes its deadline from a literal; rewrite that one number so
    # the loop's structure -- the thing under test -- is untouched.
    assert script.count("+ 1200 ))") == 1, "gate deadline literal moved"
    script = script.replace("+ 1200 ))", f"+ {deadline_seconds} ))")
    # The gate bounds its API call at 120s and sleeps 60s between polls. Both
    # are correct in CI and far too slow for a test, so shrink them here. The
    # STRUCTURE under test -- bound the call, then check the deadline -- is
    # untouched; only the durations change.
    # Deliberately NOT asserted unique: a mutation that removes the bound is
    # exactly what these tests must detect, and asserting the bound exists here
    # would fail the harness before the behavioural assertions ever ran --
    # reporting a guard that works when it was never exercised.
    script = script.replace("timeout 120 gh api", "timeout 2 gh api")
    assert script.count("\n  sleep 60\n") == 1, "gate poll interval moved"
    script = script.replace("\n  sleep 60\n", "\n  sleep 1\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(gh_behaviour, encoding="utf-8")
    gh.chmod(0o755)

    # macOS has no GNU `timeout`, and the workflow runs on ubuntu-latest where
    # it is present. Supplying a stub keeps this test measuring the GATE -- does
    # it bound the call and reach its deadline -- rather than the host's
    # coreutils. The stub is a real bound, not a no-op: it must kill an
    # overrunning child and exit 124, or the stall test would pass vacuously.
    timeout_stub = bin_dir / "timeout"
    timeout_stub.write_text(
        "#!/bin/sh\n"
        "limit=$1\n"
        "shift\n"
        '"$@" &\n'
        "child=$!\n"
        '( sleep "$limit"; kill -9 $child 2>/dev/null ) & killer=$!\n'
        "wait $child 2>/dev/null; status=$?\n"
        "kill $killer 2>/dev/null\n"
        "[ $status -ge 128 ] && exit 124\n"
        "exit $status\n",
        encoding="utf-8",
    )
    timeout_stub.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
        "HEAD_SHA": "0" * 40,
        "REPO": "owner/repo",
        "PR": "1",
        "GITHUB_RUN_ID": "12345",
    }
    return subprocess.run(
        ["bash", "-c", script],
        # The gate's exit code is the thing under test, so a non-zero exit must
        # come back as data rather than raise.
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=wall_clock,
    )


def test_a_stalled_api_call_still_reaches_the_deadline_message() -> None:
    """The defect: an unbounded `gh api` blocks past the deadline.

    The stub hangs the way a stalled request does. If the API call is bounded,
    the loop treats the stall as a poll that found nothing, notices the
    deadline has passed, and prints the remedy. If it is not bounded, the
    script never reaches that branch and this fails on the wall clock.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = _run_gate(
            Path(tmp),
            # Hangs for far longer than the gate's budget, as a stalled
            # connection does. `exec sleep` so `timeout` signals the sleep
            # itself rather than a shell that ignores the signal.
            gh_behaviour="#!/bin/sh\nexec sleep 600\n",
            deadline_seconds=1,
            wall_clock=60,
        )

    assert result.returncode == 1, (
        "the gate must FAIL on a stalled API, not hang until the job timeout; "
        f"got {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "@greptile-apps review" in result.stdout, (
        "the deadline branch never ran, so the contributor gets a bare job "
        f"timeout with no remedy\nstdout:\n{result.stdout}"
    )
    assert "gh run rerun" in result.stdout, (
        "the remedy must name the rerun command; a fast fail that does not say "
        f"how to recover is not an improvement\nstdout:\n{result.stdout}"
    )


def test_a_five_of_five_review_of_head_still_passes() -> None:
    """The bound must not break the success path.

    A timeout wrapper whose non-zero exit propagates, or one that truncates the
    response, would fail a pull request that Greptile has actually approved.
    This is the acceptance half: without it, the guard above is satisfied by a
    gate that fails everything.
    """
    import tempfile

    head = "0" * 40
    payload = json.dumps(
        [
            {
                "user": {"login": "greptile-apps[bot]"},
                # A real body is multiline; the gate base64s it for that reason,
                # so a single-line fixture would not exercise the same path.
                "body": f"Confidence Score: 5/5\nLast reviewed commit: {head}",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ]
    )
    # The gate calls `gh api .../comments` for candidates. The commit-resolution
    # call is not reached, because this fixture names a full-length SHA.
    stub = "#!/bin/sh\ncat <<'JSON'\n" + payload + "\nJSON\n"

    with tempfile.TemporaryDirectory() as tmp:
        result = _run_gate(
            Path(tmp),
            gh_behaviour=stub,
            deadline_seconds=1200,
            wall_clock=60,
        )

    assert result.returncode == 0, (
        "a 5/5 review of the exact head must pass the gate\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "5/5" in result.stdout
