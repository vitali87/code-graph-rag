"""Every pull request must get a CodeQL result at its head, whatever its base.

OpenSSF Scorecard's SAST check credits each commit on main to the pull request
that merged it, and looks for a SAST check run at that pull request's head. A
pull request merged into another one's branch (a stacked layer, or a fix aimed
at a layer) reaches main through its parent, so if CodeQL only ran for pull
requests targeting main, its commits count as unscanned even though the parent
scanned the same code on its way in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"

# Filters that drop a pull request by its base branch or by the files it touches.
SKIPPING_FILTERS = ("branches", "branches-ignore", "paths", "paths-ignore")
# The activity types that give a pull request a new head to scan.
HEAD_EVENTS = frozenset({"opened", "synchronize", "reopened"})


def _triggers(workflow: dict) -> dict:
    # PyYAML follows YAML 1.1 and reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    return triggers or {}


def _ways_a_pr_skips_codeql(workflow: dict) -> list[str]:
    triggers = _triggers(workflow)
    if "pull_request" not in triggers:
        return ["no pull_request trigger"]
    config = triggers["pull_request"] or {}
    reasons = [f"pull_request.{key}" for key in SKIPPING_FILTERS if key in config]
    if (types := config.get("types")) is not None:
        reasons += [
            f"pull_request.types lacks {event}"
            for event in sorted(HEAD_EVENTS - set(types))
        ]
    reasons += [
        f"jobs.{name}.if"
        for name, job in (workflow.get("jobs") or {}).items()
        if "if" in job
    ]
    return reasons


def test_codeql_scans_a_pull_request_into_any_branch() -> None:
    workflow = yaml.safe_load(CODEQL_WORKFLOW.read_text(encoding="utf-8"))
    assert _ways_a_pr_skips_codeql(workflow) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "on:\n  pull_request:\n    branches: [main]\n",
            ["pull_request.branches"],
        ),
        (
            "on:\n  pull_request:\n    branches-ignore: ['pr-split/**']\n",
            ["pull_request.branches-ignore"],
        ),
        (
            "on:\n  pull_request:\n    paths: ['**/*.py']\n",
            ["pull_request.paths"],
        ),
        (
            "on:\n  pull_request:\n    paths-ignore: ['docs/**']\n",
            ["pull_request.paths-ignore"],
        ),
        (
            "on:\n  pull_request:\n    types: [opened]\n",
            [
                "pull_request.types lacks reopened",
                "pull_request.types lacks synchronize",
            ],
        ),
        (
            "on:\n  push:\n    branches: [main]\n",
            ["no pull_request trigger"],
        ),
        (
            "on: pull_request\njobs:\n  analyze:\n    if: github.base_ref == 'main'\n",
            ["jobs.analyze.if"],
        ),
    ],
)
def test_a_workflow_that_can_skip_a_pull_request_is_refused(
    source: str, expected: list[str]
) -> None:
    assert _ways_a_pr_skips_codeql(yaml.safe_load(source)) == expected


@pytest.mark.parametrize(
    "source",
    [
        "on: pull_request\n",
        "on: [push, pull_request]\n",
        "on:\n  pull_request:\n"
        "    types: [opened, synchronize, reopened, ready_for_review]\n",
    ],
)
def test_an_unfiltered_pull_request_trigger_is_accepted(source: str) -> None:
    assert _ways_a_pr_skips_codeql(yaml.safe_load(source)) == []
