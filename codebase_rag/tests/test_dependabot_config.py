"""Dependabot watches every manifest the project ships or builds with."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs

REPO_ROOT = Path(__file__).resolve().parents[2]

_WATCHED_MANIFESTS = [
    ("uv", "/"),
    ("docker", "/"),
    ("docker", "/.clusterfuzzlite"),
    ("docker", "/.oss-scanner"),
    ("pip", "/scripts"),
    ("pip", "/.clusterfuzzlite"),
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


def _ignores_for(ecosystem: str, directory: str) -> list[dict[str, object]]:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding=cs.ENCODING_UTF8)
    )
    for update in config["updates"]:
        directories = update.get("directories") or [update["directory"]]
        if update["package-ecosystem"] == ecosystem and directory in directories:
            return list(update.get("ignore") or [])
    return []


def test_the_ts_oracle_is_held_below_typescript_7() -> None:
    # TypeScript 7 is the native port: its package no longer exports the
    # compiler API (`createSourceFile`, `ScriptTarget`) the oracle is built on.
    ignores = _ignores_for("npm", "/evals/oracles/ts_oracle")
    assert {"dependency-name": "typescript", "versions": [">=7"]} in ignores


def test_typescript_6_updates_still_reach_the_ts_oracle() -> None:
    # Negative: the hold is on the major that broke the API, not on every
    # TypeScript release, so 6.x patches keep arriving.
    ignores = _ignores_for("npm", "/evals/oracles/ts_oracle")
    typescript = [i for i in ignores if i.get("dependency-name") == "typescript"]
    assert all(i.get("versions") == [">=7"] for i in typescript)
    assert all("update-types" not in i for i in typescript)


def test_the_scanner_image_stays_on_python_3_12() -> None:
    # The image pins UV_PYTHON=python3.12 with downloads off, so the 3.15 base
    # Dependabot proposed (#3461) left `uv sync` no interpreter to build with.
    ignores = _ignores_for("docker", "/.oss-scanner")
    assert {
        "dependency-name": "python",
        "update-types": [
            "version-update:semver-minor",
            "version-update:semver-major",
        ],
    } in ignores


def test_the_production_image_still_gets_python_updates() -> None:
    # Negative: the hold is the scanner image's alone, so the production
    # image's python base keeps moving.
    ignores = _ignores_for("docker", "/")
    assert not any(i.get("dependency-name") == "python" for i in ignores)


@pytest.mark.parametrize("image", ["golang", "eclipse-temurin"])
def test_the_scanner_images_other_bases_are_not_held(image: str) -> None:
    # Negative: Go and the JDK are copied in whole, so their bumps still land.
    ignores = _ignores_for("docker", "/.oss-scanner")
    assert not any(i.get("dependency-name") == image for i in ignores)


def test_the_fuzz_image_keeps_the_pyinstaller_oss_fuzz_pins() -> None:
    # base-builder's install_python.sh pins pyinstaller==6.10.0, and the fuzz
    # image installs the same one for its own Python 3.12.
    ignores = _ignores_for("pip", "/.clusterfuzzlite")
    assert {"dependency-name": "pyinstaller"} in ignores


def test_atheris_updates_still_reach_the_fuzz_image() -> None:
    # Negative: install_python.sh asks for any atheris from 2.3.0 on, so the
    # hold is on pyinstaller alone.
    ignores = _ignores_for("pip", "/.clusterfuzzlite")
    assert not any(i.get("dependency-name") == "atheris" for i in ignores)


def _github_actions_update() -> dict[str, object]:
    config = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding=cs.ENCODING_UTF8)
    )
    return next(
        u for u in config["updates"] if u["package-ecosystem"] == "github-actions"
    )


def _codeql_group_patterns() -> list[str]:
    groups = _github_actions_update().get("groups") or {}
    return list(groups["codeql-action"]["patterns"])


@pytest.mark.parametrize("step", ["init", "analyze", "upload-sarif"])
def test_the_codeql_action_steps_move_together(step: str) -> None:
    # analyze refuses a configuration init wrote under another release, so
    # init bumped alone to 4.39.1 failed every CodeQL job (#3455).
    name = f"github/codeql-action/{step}"
    assert any(fnmatch(name, p) for p in _codeql_group_patterns())


@pytest.mark.parametrize("action", ["actions/checkout", "github/super-linter"])
def test_other_actions_are_not_grouped_with_codeql(action: str) -> None:
    # Negative: the group is for CodeQL's own steps, so every other action
    # still arrives in a PR of its own.
    assert not any(fnmatch(action, p) for p in _codeql_group_patterns())


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
