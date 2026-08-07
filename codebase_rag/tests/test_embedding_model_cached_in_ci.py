"""Every workflow that runs the unit suite must cache the embedding model.

`sonarcloud.yml` runs the same `pytest -m "not integration"` as `ci.yml`, so a
fix applied to only one of them leaves the other downloading weights mid-suite
(issue #1092) — which is exactly how it failed on PR #1113.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _jobs_running_the_unit_suite() -> list[tuple[str, str, list[dict]]]:
    found = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            steps = job.get("steps") or []
            script = "\n".join(step.get("run", "") for step in steps)
            if 'pytest -n auto -m "not integration"' in script:
                found.append((path.name, job_name, steps))
    return found


def test_at_least_one_job_runs_the_unit_suite() -> None:
    assert _jobs_running_the_unit_suite()


@pytest.mark.parametrize(
    ("workflow", "job"),
    [(w, j) for w, j, _ in _jobs_running_the_unit_suite()],
)
def test_unit_suite_jobs_cache_the_embedding_model(workflow: str, job: str) -> None:
    steps = next(
        s for w, j, s in _jobs_running_the_unit_suite() if w == workflow and j == job
    )
    uses = " ".join(step.get("uses", "") for step in steps)
    script = "\n".join(step.get("run", "") for step in steps)

    assert "actions/cache" in uses, (
        f"{workflow}:{job} runs the unit suite without caching the HuggingFace "
        "hub, so it downloads the embedding model on every run"
    )
    assert "snapshot_download" in script, (
        f"{workflow}:{job} does not prefetch the embedding model, so a hub "
        "failure surfaces mid-pytest instead of in a named step"
    )
