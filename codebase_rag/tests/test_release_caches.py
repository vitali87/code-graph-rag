"""Guards against publishing what a restored cache could have changed.

A GitHub Actions cache entry is readable from every later run on the same
ref and from runs on any ref that falls back to the default branch, and any
step of any run there can write one. So a run whose output is published
must restore no cache.

``rust-wheels.yml`` is reached from ``publish.yml`` through
``workflow_call``, so it runs under the caller's event: ``release``, a tag
push or ``workflow_dispatch``. Every job in that run counts, not only the
wheel builds: ``publish-rust`` downloads every artifact matching
``cgr-graph-rs-*``, and any job's steps can upload one.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RUST_WHEELS = WORKFLOWS / "rust-wheels.yml"
DOCKER_PUBLISH = WORKFLOWS / "docker-publish.yml"

SETUP_UV = "astral-sh/setup-uv@"
UPLOAD_ARTIFACT = "actions/upload-artifact@"
# The events publish.yml runs on, which a workflow it calls inherits, each
# with whether it is a tag push.
PUBLISHING_EVENTS = (("release", False), ("push", True), ("workflow_dispatch", False))
# setup-uv's default, "auto", skips the cache on these events only.
AUTO_SKIPS = {"release", "pull_request_target", "workflow_run"}
EVENT_IS = re.compile(r"^\$\{\{\s*github\.event_name\s*==\s*'([a-z_]+)'\s*\}\}$")


def _jobs(path: Path) -> dict[str, dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _restores_uv_cache(step: dict, event: str, *, tag: bool) -> bool:
    # A model of setup-uv's `enable-cache` for the forms this repository
    # uses; any other form fails the test rather than reading as "off".
    value = step.get("with", {}).get("enable-cache", "auto")
    if isinstance(value, bool):
        return value
    if value == "auto":
        return event not in AUTO_SKIPS and not (event == "push" and tag)
    match = EVENT_IS.match(value)
    assert match, f"unrecognised enable-cache form {value!r}"
    return event == match.group(1)


def _setup_uv_steps(path: Path) -> list[tuple[str, dict]]:
    return [
        (job_id, step)
        for job_id, job in _jobs(path).items()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith(SETUP_UV)
    ]


def test_every_job_that_uploads_from_rust_wheels_sets_up_uv() -> None:
    # The cache rule below covers what it can see: a job that uploads an
    # artifact but installs uv some other way would escape it unnoticed.
    uploaders = {
        job_id
        for job_id, job in _jobs(RUST_WHEELS).items()
        if any(
            step.get("uses", "").startswith(UPLOAD_ARTIFACT)
            for step in job.get("steps", [])
        )
    }

    assert uploaders == {"linux", "macos", "windows"}
    assert uploaders <= {job_id for job_id, _ in _setup_uv_steps(RUST_WHEELS)}


def test_rust_wheels_restores_no_uv_cache_when_publish_calls_it() -> None:
    steps = _setup_uv_steps(RUST_WHEELS)
    assert steps

    for job_id, step in steps:
        for event, tag in PUBLISHING_EVENTS:
            assert not _restores_uv_cache(step, event, tag=tag), (
                f"{job_id} restores a uv cache on {event}"
            )


def test_pull_requests_keep_the_cache_for_the_wheel_tests() -> None:
    cached = {
        job_id
        for job_id, step in _setup_uv_steps(RUST_WHEELS)
        if _restores_uv_cache(step, "pull_request", tag=False)
    }

    assert cached == {"test", "test-musl"}


def test_the_published_image_is_built_without_a_restored_cache() -> None:
    for job_id, job in _jobs(DOCKER_PUBLISH).items():
        for step in job.get("steps", []):
            assert "cache-from" not in step.get("with", {}), (
                f"{job_id}: {step.get('uses') or step.get('name')} restores a "
                "build cache into the image it pushes"
            )
