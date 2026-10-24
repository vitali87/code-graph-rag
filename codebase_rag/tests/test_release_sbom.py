"""Guards for the SBOM published beside each PyInstaller binary.

The SBOM is read from its own environment rather than the build's, so the
two can drift apart without anything failing: an extra added to the build
but not to the SBOM leaves its packages out of every SBOM.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-binaries.yml"

BUILD_JOB_ID = "build"
INSTALL_STEP_NAME = "Install dependencies"
SBOM_STEP_NAME = "Generate SBOM"
BINARY_ARTIFACT_PREFIX = "dist/code-graph-rag-"

EXTRA_FLAG = re.compile(r"--extra\s+(\S+)")


def _steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return workflow["jobs"][BUILD_JOB_ID]["steps"]


def _step(name: str) -> dict:
    return next(s for s in _steps() if s.get("name") == name)


def test_sbom_step_runs_after_build_and_before_upload() -> None:
    names = [step.get("name") for step in _steps()]

    assert names.index("Build binary") < names.index(SBOM_STEP_NAME)
    assert names.index(SBOM_STEP_NAME) < names.index("Upload binary artifact")


def test_sbom_output_matches_the_release_globs() -> None:
    assert f'--output-file "{BINARY_ARTIFACT_PREFIX}' in _step(SBOM_STEP_NAME)["run"], (
        "the upload, release and signing steps glob dist/code-graph-rag-*; "
        "an SBOM outside that prefix is built and then dropped"
    )


def test_sbom_environment_has_the_build_extras_and_no_dev_group() -> None:
    sbom = _step(SBOM_STEP_NAME)["run"]

    assert "--no-dev" in sbom
    assert set(EXTRA_FLAG.findall(sbom)) == set(
        EXTRA_FLAG.findall(_step(INSTALL_STEP_NAME)["run"])
    )
