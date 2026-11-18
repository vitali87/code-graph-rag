"""No workflow job runs on a GitHub-hosted runner image GitHub has retired.

A job on a retired label is not refused when the workflow is pushed: it is
queued and then cancelled, first during the brownouts before the retirement
date and on every run after it. The release workflows only run on a tag, so
the first sign is a release that ships without its macOS wheel and binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))

# https://github.com/actions/runner-images: each of these is past its
# retirement date (macos-14 on 2 November 2026).
RETIRED_IMAGES = frozenset({"macos-13", "macos-14", "ubuntu-20.04", "windows-2019"})

type YamlNode = dict[str, YamlNode] | list[YamlNode] | str | int | float | bool | None


def _retired_labels(node: YamlNode) -> list[str]:
    """Every value naming a retired image, wherever a job takes its runner
    from: `runs-on` as a string or a list, or a matrix entry it expands."""
    match node:
        case dict():
            return [
                label for value in node.values() for label in _retired_labels(value)
            ]
        case list():
            return [label for value in node for label in _retired_labels(value)]
        case str() if node in RETIRED_IMAGES:
            return [node]
        case _:
            return []


def _workflow_problems(text: str) -> list[str]:
    jobs = yaml.safe_load(text).get("jobs", {})
    return [
        f"job {name} runs on retired {label}"
        for name, job in jobs.items()
        for label in _retired_labels(job)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  build:\n    runs-on: macos-14\n",
        "jobs:\n  build:\n    runs-on: [self-hosted, macos-13]\n",
        (
            "jobs:\n  build:\n    runs-on: ${{ matrix.os }}\n"
            "    strategy:\n      matrix:\n        include:\n"
            "          - os: ubuntu-latest\n          - os: macos-14\n"
        ),
        "jobs:\n  test:\n    strategy:\n      matrix:\n        os: [windows-2019]\n",
    ],
    ids=["runs-on", "runs-on-list", "matrix-include", "matrix-list"],
)
def test_a_job_on_a_retired_image_is_rejected(text: str) -> None:
    assert len(_workflow_problems(text)) == 1


def test_current_images_and_prose_naming_a_retired_one_are_accepted() -> None:
    text = (
        "jobs:\n  build:\n    runs-on: ${{ matrix.os }}\n"
        "    strategy:\n      matrix:\n        include:\n"
        "          - os: macos-15\n          - os: macos-15-intel\n"
        "    steps:\n      - run: echo moved off macos-14\n"
    )
    assert _workflow_problems(text) == []


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_no_workflow_runs_on_a_retired_image(workflow: Path) -> None:
    assert _workflow_problems(workflow.read_text(encoding="utf-8")) == []


def test_the_workflows_were_found() -> None:
    names = {path.name for path in WORKFLOWS}
    assert {"rust-wheels.yml", "build-binaries.yml"} <= names
