"""Linux jobs use the runner pool when it has room, and only where that is safe.

infra/runner-pool adds the maintainer's own servers as extra CI capacity. A
job reaches them only through one exact `runs-on` expression: it reads the
OWN_RUNNERS_FREE variable the pool's switch keeps up to date, and it keeps
fork pull requests and release builds on GitHub's machines whatever that
variable says. A job written with a plain `ubuntu-latest` never uses the
pool, and one written with a bare `self-hosted` label would hand fork code to
the servers, so every job on Linux either carries the expression or is
listed below with the reason it stays on GitHub.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
POOL_LIB = REPO_ROOT / "infra" / "runner-pool" / "bin" / "lib.sh"

POOL_LABEL = "own-linux"
SWITCH_VARIABLE = "OWN_RUNNERS_FREE"

_GUARD = (
    f"vars.{SWITCH_VARIABLE} == 'true' && github.event_name != 'release'"
    " && !startsWith(github.ref, 'refs/tags/')"
    " && (github.event_name != 'pull_request'"
    " || github.event.pull_request.head.repo.full_name == github.repository)"
    f' && fromJSON(\'["self-hosted","{POOL_LABEL}"]\')'
)
POOL_RUNS_ON = f"${{{{ {_GUARD} || 'ubuntu-latest' }}}}"
POOL_MATRIX_RUNS_ON = (
    f"${{{{ matrix.os == 'ubuntu-latest' && {_GUARD} || matrix.os }}}}"
)
GUARDED = frozenset({POOL_RUNS_ON, POOL_MATRIX_RUNS_ON})

# (workflow file, job): why it stays on GitHub's machines.
STAYS_ON_GITHUB = {
    ("build-binaries.yml", "sign-release"): "signs and attests release files",
    ("copilot-code-review.yml", "copilot-setup-steps"): (
        "Copilot's coding agent takes its machine from this job"
    ),
    ("deferred-release.yml", "publish"): "publishes a release",
    ("docker-publish.yml", "build-and-push"): "builds and pushes the release image",
    ("docs.yml", "build"): "builds the published docs site",
    ("docs.yml", "deploy"): "deploys the docs site",
    ("publish.yml", "ci-gate"): "part of publishing a release",
    ("publish.yml", "rust-version"): "part of publishing a release",
    ("publish.yml", "publish-rust"): "uploads to PyPI",
    ("publish.yml", "publish"): "uploads to PyPI",
    ("publish.yml", "publish-mcp-registry"): "publishes to the MCP registry",
    ("rust-wheels.yml", "linux"): "builds the wheels publish.yml uploads",
    ("scorecard.yml", "analysis"): (
        "Scorecard only accepts results from GitHub's machines"
    ),
}

# Events only the maintainers, or their merges, can start. pull_request and
# release are here because the expression itself keeps forks and releases
# off the pool.
TRUSTED_TRIGGERS = frozenset(
    {
        "push",
        "pull_request",
        "release",
        "workflow_dispatch",
        "schedule",
        "merge_group",
        "workflow_call",
    }
)

type YamlNode = dict[str, YamlNode] | list[YamlNode] | str | int | float | bool | None
type Workflow = dict[str | bool, YamlNode]


def _strings(node: YamlNode) -> list[str]:
    match node:
        case dict():
            return [text for value in node.values() for text in _strings(value)]
        case list():
            return [text for value in node for text in _strings(value)]
        case str():
            return [node]
        case _:
            return []


def _triggers(workflow: Workflow) -> set[str]:
    # PyYAML reads the bare key `on` as the boolean True.
    match workflow.get("on", workflow.get(True)):
        case str() as event:
            return {event}
        case list() as events:
            return {str(event) for event in events}
        case dict() as events:
            return set(events)
        case _:
            return set()


def _may_run_on_linux(job: dict[str, YamlNode]) -> bool:
    match job.get("runs-on"):
        case "ubuntu-latest":
            return True
        case "${{ matrix.os }}":
            return "ubuntu-latest" in _strings(job.get("strategy"))
        case _:
            return False


def _pool_problems(name: str, workflow: Workflow) -> list[str]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    problems: list[str] = []
    uses_pool = False
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        runs_on = job.get("runs-on")
        if isinstance(runs_on, str) and runs_on in GUARDED:
            uses_pool = True
            if (name, job_name) in STAYS_ON_GITHUB:
                problems.append(f"job {job_name} is listed to stay on GitHub")
            continue
        if any(
            "self-hosted" in label or POOL_LABEL in label for label in _strings(runs_on)
        ):
            problems.append(f"job {job_name} names the pool without the guard")
        elif _may_run_on_linux(job) and (name, job_name) not in STAYS_ON_GITHUB:
            problems.append(
                f"job {job_name} runs on ubuntu-latest: give it the pool"
                " expression, or list it in STAYS_ON_GITHUB with the reason"
            )
    if uses_pool and (untrusted := _triggers(workflow) - TRUSTED_TRIGGERS):
        problems.append(f"uses the pool but also starts on {sorted(untrusted)}")
    return problems


def _load(text: str) -> Workflow:
    return yaml.safe_load(text)


@pytest.mark.parametrize(
    "workflow",
    [
        _load("on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"),
        _load("on: push\njobs:\n  build:\n    runs-on: [self-hosted, own-linux]\n"),
        {
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "${{ vars.OWN_RUNNERS_FREE == 'true'"
                    ' && fromJSON(\'["self-hosted","own-linux"]\')'
                    " || 'ubuntu-latest' }}"
                }
            },
        },
        {"on": ["issue_comment"], "jobs": {"build": {"runs-on": POOL_RUNS_ON}}},
        _load(
            "on: push\njobs:\n  build:\n    runs-on: ${{ matrix.os }}\n"
            "    strategy:\n      matrix:\n        os: [ubuntu-latest, macos-15]\n"
        ),
    ],
    ids=["plain", "bare-label", "no-fork-guard", "untrusted-trigger", "matrix"],
)
def test_an_unguarded_or_unrouted_linux_job_is_rejected(workflow: Workflow) -> None:
    assert len(_pool_problems("example.yml", workflow)) == 1


def test_the_guarded_expressions_and_other_machines_are_accepted() -> None:
    workflow: Workflow = {
        True: {"pull_request": None},
        "jobs": {
            "lint": {"runs-on": POOL_RUNS_ON},
            "test": {
                "runs-on": POOL_MATRIX_RUNS_ON,
                "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest"]}},
            },
            "mac": {"runs-on": "macos-15"},
        },
    }
    assert _pool_problems("example.yml", workflow) == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_every_linux_job_uses_the_pool_or_says_why_not(path: Path) -> None:
    assert _pool_problems(path.name, _load(path.read_text(encoding="utf-8"))) == []


def test_every_job_listed_to_stay_on_github_exists() -> None:
    jobs = set()
    for path in WORKFLOWS:
        match _load(path.read_text(encoding="utf-8")).get("jobs"):
            case dict() as named:
                jobs |= {(path.name, job) for job in named}
            case _:
                pass
    assert set(STAYS_ON_GITHUB) <= jobs


def test_the_workflows_and_the_pool_agree_on_names() -> None:
    lib = POOL_LIB.read_text(encoding="utf-8")
    assert re.search(rf"^POOL_LABEL={POOL_LABEL}$", lib, re.MULTILINE)
    assert re.search(rf"^SWITCH_VARIABLE={SWITCH_VARIABLE}$", lib, re.MULTILINE)
