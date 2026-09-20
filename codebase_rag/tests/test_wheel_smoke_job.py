"""Guards for the unlocked-resolution CI job (issue #1096).

Every other CI job installs from ``uv.lock``, so a dependency shipping a
breaking major inside our declared floors reaches PyPI unnoticed. The job and
its smoke script are the only thing standing between that and a user's
terminal, so both are pinned here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_wheel.py"

JOB_ID = "wheel-fresh-resolution"
GATE_JOB_ID = "all-checks-pass"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _job_run_script(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


@pytest.fixture(scope="module")
def smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_wheel", SMOKE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_wheel"] = module
    spec.loader.exec_module(module)
    return module


class TestWorkflowJob:
    def test_job_exists(self) -> None:
        assert JOB_ID in _workflow()["jobs"], (
            f"{JOB_ID} is the only CI job that resolves dependencies the way a "
            "fresh `pip install code-graph-rag` does"
        )

    def test_job_builds_a_wheel_and_installs_it_without_the_lock(self) -> None:
        script = _job_run_script(_workflow()["jobs"][JOB_ID])

        assert "uv build" in script
        assert "pip install dist/" in script
        assert "uv sync" not in script, (
            "uv sync installs from uv.lock, which is exactly the resolution "
            "this job exists to avoid"
        )
        assert "uv run" not in script, (
            "uv run resolves against uv.lock; the smoke must run from the "
            "freshly resolved venv's interpreter"
        )

    def test_checkout_does_not_persist_credentials(self) -> None:
        # The job installs freshly resolved third-party packages and then runs
        # repository code; a persisted workflow token would be readable by it.
        checkout = next(
            step
            for step in _workflow()["jobs"][JOB_ID]["steps"]
            if step.get("uses", "").startswith("actions/checkout")
        )

        assert checkout.get("with", {}).get("persist-credentials") is False

    def test_job_installs_into_an_isolated_venv(self) -> None:
        # The isolation is the point: the wheel must be exercised from a fresh
        # interpreter, not from the lock-synced dev environment.
        script = _job_run_script(_workflow()["jobs"][JOB_ID])

        assert "python -m venv" in script
        assert "./.fresh-venv/bin/python -m pip install dist/" in script
        assert "./.fresh-venv/bin/python" in script, (
            "the smoke must run on the fresh venv's interpreter"
        )

    def test_job_runs_the_smoke_script(self) -> None:
        script = _job_run_script(_workflow()["jobs"][JOB_ID])

        assert "scripts/smoke_wheel.py" in script
        assert "cgr --version" in script

    def test_failure_blocks_the_aggregate_gate(self) -> None:
        gate = _workflow()["jobs"][GATE_JOB_ID]

        assert JOB_ID in gate["needs"], (
            f"{GATE_JOB_ID} must depend on {JOB_ID}, or the job can fail "
            "without blocking a merge"
        )
        assert f"needs.{JOB_ID}.result" in _job_run_script(gate), (
            f"{GATE_JOB_ID} lists {JOB_ID} but never checks its result"
        )


class TestSmokeScript:
    def test_script_exists_and_defines_checks(self, smoke_module: ModuleType) -> None:
        assert smoke_module.CHECKS, "the smoke script runs no checks"

    def test_usage_property_check_passes_on_the_installed_pydantic_ai(
        self, smoke_module: ModuleType
    ) -> None:
        # RED on pydantic-ai < 2.0, where `usage` is still callable (#964).
        smoke_module.check_agent_run_result_usage_is_a_property()

    def test_agent_loop_check_passes_on_the_installed_pydantic_ai(
        self, smoke_module: ModuleType
    ) -> None:
        smoke_module.check_agent_loop_reports_usage()

    def test_main_reports_failure_when_a_check_raises(
        self, smoke_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> None:
            raise smoke_module.SmokeFailure("simulated dependency break")

        monkeypatch.setattr(smoke_module, "CHECKS", (broken,))

        assert smoke_module.main() == 1

    def test_main_succeeds_when_every_check_passes(
        self, smoke_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smoke_module, "CHECKS", (lambda: None,))

        assert smoke_module.main() == 0
