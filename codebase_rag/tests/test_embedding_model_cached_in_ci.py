"""Every workflow that runs the unit suite must cache the embedding model.

`sonarcloud.yml` runs the same `pytest -m "not integration"` as `ci.yml`, so a
fix applied to only one of them leaves the other downloading weights mid-suite
(issue #1092) — which is exactly how it failed on PR #1113.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codebase_rag.utils import dependencies

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


UNIT_SUITE_MARKER = 'pytest -n auto -m "not integration"'
HUB_CACHE_PATH = "~/.cache/huggingface"
EMBEDDING_MODEL = "microsoft/unixcoder-base"


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])


def _step_script(step: dict) -> str:
    return step.get("run", "") or ""


def _jobs_running_the_unit_suite() -> list[tuple[str, str, list[dict]]]:
    found = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            steps = job.get("steps") or []
            if any(UNIT_SUITE_MARKER in _step_script(step) for step in steps):
                found.append((path.name, job_name, steps))
    return found


def _steps_for(workflow: str, job: str) -> list[dict]:
    return next(
        steps
        for name, job_name, steps in _jobs_running_the_unit_suite()
        if name == workflow and job_name == job
    )


def test_at_least_one_job_runs_the_unit_suite() -> None:
    assert _jobs_running_the_unit_suite()


@pytest.mark.parametrize(
    ("workflow", "job"),
    [(w, j) for w, j, _ in _jobs_running_the_unit_suite()],
)
def test_unit_suite_jobs_cache_the_hub_directory(workflow: str, job: str) -> None:
    caches = [
        step
        for step in _steps_for(workflow, job)
        if step.get("uses", "").startswith("actions/cache")
        and HUB_CACHE_PATH in str(step.get("with", {}).get("path", ""))
    ]

    assert caches, (
        f"{workflow}:{job} runs the unit suite without caching {HUB_CACHE_PATH}, "
        "so it re-downloads the embedding model on every run"
    )


@pytest.mark.parametrize(
    ("workflow", "job"),
    [(w, j) for w, j, _ in _jobs_running_the_unit_suite()],
)
def test_unit_suite_jobs_prefetch_this_model_before_pytest(
    workflow: str, job: str
) -> None:
    steps = _steps_for(workflow, job)
    prefetch_at = [
        index
        for index, step in enumerate(steps)
        if f'snapshot_download("{EMBEDDING_MODEL}")' in _step_script(step)
        or f"snapshot_download('{EMBEDDING_MODEL}')" in _step_script(step)
    ]
    pytest_at = [
        index
        for index, step in enumerate(steps)
        if UNIT_SUITE_MARKER in _step_script(step)
    ]

    assert prefetch_at, (
        f"{workflow}:{job} does not prefetch {EMBEDDING_MODEL}, so a hub "
        "failure surfaces mid-pytest instead of in a named step"
    )
    assert min(prefetch_at) < min(pytest_at), (
        f"{workflow}:{job} prefetches after running the suite, which defeats it"
    )


@pytest.mark.parametrize(
    ("workflow", "job"),
    [(w, j) for w, j, _ in _jobs_running_the_unit_suite()],
)
def test_the_prefetch_retries_transient_hub_failures(workflow: str, job: str) -> None:
    # 429 Too Many Requests is the reported transient; one attempt turns it
    # into a red build.
    script = "\n".join(
        _step_script(step)
        for step in _steps_for(workflow, job)
        if "snapshot_download" in _step_script(step)
    )

    assert "for attempt in" in script, (
        f"{workflow}:{job} prefetches without retrying, so a single 429 fails the build"
    )


class TestLocalWeightsProbe:
    """`has_local_embedding_weights()` must answer without touching the network.

    In CI the weights are prefetched, so the negative branches never execute
    there; they are exercised here explicitly.
    """

    def test_false_when_ml_dependencies_are_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dependencies, "has_torch", lambda: False)

        assert dependencies.has_local_embedding_weights() is False

    def test_false_when_transformers_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dependencies, "has_torch", lambda: True)
        monkeypatch.setattr(dependencies, "has_transformers", lambda: False)

        assert dependencies.has_local_embedding_weights() is False

    @pytest.mark.parametrize("missing", ["AutoConfig", "AutoTokenizer", "AutoModel"])
    def test_false_when_any_artifact_will_not_resolve(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        # A cache holding only some of UniXcoder's files must not report ready:
        # config.json alone satisfies AutoConfig and then fails in the embedder.
        transformers = pytest.importorskip("transformers")
        monkeypatch.setattr(dependencies, "has_torch", lambda: True)
        monkeypatch.setattr(dependencies, "has_transformers", lambda: True)
        reached: list[str] = []

        def _stub(name: str) -> object:
            def inner(*_args: object, **_kwargs: object) -> object:
                reached.append(name)
                if name == missing:
                    raise OSError(f"{name} not in the local cache")
                return object()

            return inner

        # Every loader is stubbed, not just the failing one: on a machine with
        # no cache an earlier REAL loader raises first, and the case then
        # passes without ever reaching the artifact it is named for.
        for name in ("AutoConfig", "AutoTokenizer", "AutoModel"):
            monkeypatch.setattr(
                getattr(transformers, name), "from_pretrained", _stub(name)
            )

        assert dependencies.has_local_embedding_weights() is False
        assert missing in reached, (
            f"the probe never reached {missing}, so this case proved nothing"
        )

    def test_true_when_every_artifact_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transformers = pytest.importorskip("transformers")
        monkeypatch.setattr(dependencies, "has_torch", lambda: True)
        monkeypatch.setattr(dependencies, "has_transformers", lambda: True)
        for name in ("AutoConfig", "AutoTokenizer", "AutoModel"):
            monkeypatch.setattr(
                getattr(transformers, name),
                "from_pretrained",
                lambda *_a, **_k: object(),
            )

        assert dependencies.has_local_embedding_weights() is True

    def test_the_probe_never_asks_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every resolution must pass local_files_only, or the probe becomes the
        # very download it exists to prevent.
        transformers = pytest.importorskip("transformers")
        monkeypatch.setattr(dependencies, "has_torch", lambda: True)
        monkeypatch.setattr(dependencies, "has_transformers", lambda: True)
        seen: list[dict[str, object]] = []

        def _record(*_args: object, **kwargs: object) -> object:
            seen.append(kwargs)
            return object()

        for name in ("AutoConfig", "AutoTokenizer", "AutoModel"):
            monkeypatch.setattr(getattr(transformers, name), "from_pretrained", _record)

        dependencies.has_local_embedding_weights()

        assert len(seen) == 3
        assert all(kwargs.get("local_files_only") is True for kwargs in seen), seen
