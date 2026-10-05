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


def _npm_ignores_for(directory: str) -> list[dict[str, object]]:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding=cs.ENCODING_UTF8)
    )
    for update in config["updates"]:
        directories = update.get("directories") or [update["directory"]]
        if update["package-ecosystem"] == "npm" and directory in directories:
            return list(update.get("ignore") or [])
    return []


def test_the_ts_oracle_is_held_below_typescript_7() -> None:
    # TypeScript 7 is the native port: its package no longer exports the
    # compiler API (`createSourceFile`, `ScriptTarget`) the oracle is built on.
    ignores = _npm_ignores_for("/evals/oracles/ts_oracle")
    assert {"dependency-name": "typescript", "versions": [">=7"]} in ignores


def test_typescript_6_updates_still_reach_the_ts_oracle() -> None:
    # Negative: the hold is on the major that broke the API, not on every
    # TypeScript release, so 6.x patches keep arriving.
    ignores = _npm_ignores_for("/evals/oracles/ts_oracle")
    typescript = [i for i in ignores if i.get("dependency-name") == "typescript"]
    assert all(i.get("versions") == [">=7"] for i in typescript)
    assert all("update-types" not in i for i in typescript)


_ROSLYN_DIR = "/codebase_rag/parsers/csharp_frontend/roslyn"


def _nuget_update() -> dict[str, object]:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding=cs.ENCODING_UTF8)
    )
    return next(
        u
        for u in config["updates"]
        if u["package-ecosystem"] == "nuget" and u.get("directory") == _ROSLYN_DIR
    )


def test_the_roslyn_packages_move_together() -> None:
    # Each pins Workspaces.Common to its own exact version, so one bumped
    # alone cannot restore beside the other (the 5.9 update alone broke it).
    groups = _nuget_update().get("groups") or {}
    assert any(
        "Microsoft.CodeAnalysis.*" in group.get("patterns", [])
        for group in groups.values()
    )


def test_the_roslyn_frontend_stays_on_net8_packages() -> None:
    # From 5.9 Workspaces.MSBuild ships net10.0 only, and MSBuild 18 is the
    # .NET 10 SDK's; the frontend targets net8.0.
    ignores = _nuget_update().get("ignore") or []
    assert {
        "dependency-name": "Microsoft.CodeAnalysis.*",
        "versions": [">=5.9"],
    } in ignores
    assert {
        "dependency-name": "Microsoft.Build.Framework",
        "versions": [">=18"],
    } in ignores


def test_the_msbuild_locator_is_not_held() -> None:
    # Negative: the Locator has no framework floor, so its updates keep coming.
    ignores = _nuget_update().get("ignore") or []
    assert not any(
        i.get("dependency-name") == "Microsoft.Build.Locator" for i in ignores
    )
