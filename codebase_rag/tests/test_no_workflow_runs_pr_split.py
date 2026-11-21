"""No workflow runs the pr-split action.

Stacked pull requests here are native GitHub stacks managed with `gh stack`,
and the pull request size limit is checked by the agent before a pull request
is opened. A workflow still calling `vitali87/pr-split` would score every pull
request against a tool nothing else here uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))

PR_SPLIT = "vitali87/pr-split"


type YamlNode = dict[str, YamlNode] | list[YamlNode] | str | int | float | bool | None


def _is_pr_split(uses: YamlNode) -> bool:
    """A reusable workflow kept inside the pr-split repo runs the same tool, so
    a path under it counts as well as the action itself."""
    if not isinstance(uses, str):
        return False
    target = uses.split("@", 1)[0]
    return target == PR_SPLIT or target.startswith(f"{PR_SPLIT}/")


def _pr_split_uses(text: str) -> list[str]:
    jobs = yaml.safe_load(text).get("jobs") or {}
    found = [
        f"job {name}" for name, job in jobs.items() if _is_pr_split(job.get("uses"))
    ]
    found += [
        f"job {name} step {step.get('name', step['uses'])}"
        for name, job in jobs.items()
        for step in job.get("steps") or []
        if _is_pr_split(step.get("uses"))
    ]
    return found


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  score:\n    steps:\n      - uses: vitali87/pr-split@95cdcd97\n",
        (
            "jobs:\n  score:\n    steps:\n      - name: score\n"
            "        uses: vitali87/pr-split@v1.0.1\n"
        ),
        "jobs:\n  score:\n    uses: vitali87/pr-split/.github/workflows/score.yml@v1\n",
    ],
)
def test_a_workflow_running_pr_split_is_caught(text: str) -> None:
    assert _pr_split_uses(text)


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v7\n",
        "jobs:\n  build:\n    steps:\n      - uses: vitali87/pr-splitter@v1\n",
        "jobs:\n  build:\n    steps:\n      - run: echo vitali87/pr-split\n",
    ],
)
def test_a_workflow_without_pr_split_is_not_flagged(text: str) -> None:
    assert _pr_split_uses(text) == []


def test_the_workflows_are_found() -> None:
    assert WORKFLOWS, "no workflow files found, so the check below would pass vacuously"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_no_workflow_runs_pr_split(workflow: Path) -> None:
    assert _pr_split_uses(workflow.read_text(encoding="utf-8")) == []
