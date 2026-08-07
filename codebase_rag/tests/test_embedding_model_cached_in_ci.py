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

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError(f"{missing} not in the local cache")

        monkeypatch.setattr(getattr(transformers, missing), "from_pretrained", _raise)

        assert dependencies.has_local_embedding_weights() is False

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
