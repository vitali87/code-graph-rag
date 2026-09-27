"""Dependabot watches every manifest the project ships or builds with."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs

REPO_ROOT = Path(__file__).resolve().parents[2]

_WATCHED_MANIFESTS = [
    ("uv", "/"),
    ("npm", "/evals/oracles/lua_oracle"),
    ("npm", "/evals/oracles/php_oracle"),
    ("npm", "/evals/oracles/ruby_oracle"),
    ("npm", "/evals/oracles/ts_oracle"),
    ("cargo", "/evals/oracles/rs_oracle"),
    ("gomod", "/codebase_rag/parsers/go_frontend/gotypes"),
    ("nuget", "/codebase_rag/parsers/csharp_frontend/roslyn"),
]


def _watched() -> set[tuple[str, str]]:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding=cs.ENCODING_UTF8)
    )
    watched: set[tuple[str, str]] = set()
    for update in config["updates"]:
        directories = update.get("directories") or [update["directory"]]
        watched.update((update["package-ecosystem"], d) for d in directories)
    return watched


@pytest.mark.parametrize(
    ("ecosystem", "directory"),
    _WATCHED_MANIFESTS,
    ids=[f"{eco}:{directory}" for eco, directory in _WATCHED_MANIFESTS],
)
def test_every_manifest_is_watched(ecosystem: str, directory: str) -> None:
    assert (REPO_ROOT / directory.lstrip("/")).is_dir(), directory
    assert (ecosystem, directory) in _watched()


def test_the_uv_project_is_not_watched_as_pip() -> None:
    # Negative test. Under the `pip` ecosystem no version update ever reached
    # uv.lock; the one Dependabot change it received was a security update,
    # which Dependabot files under uv regardless of this config.
    assert ("pip", "/") not in _watched()
