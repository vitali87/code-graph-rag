"""What a default install pulls in, read from the lock rather than the manifest."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = "code-graph-rag"


def _runtime_closure() -> set[str]:
    # Environment markers are ignored, so this over-approximates across
    # platforms; that is the safe direction for a "never installed" check.
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    visited: set[tuple[str, str | None]] = set()
    pending: list[tuple[str, str | None]] = [(PROJECT, None)]
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        name, extra = key
        package = packages[name]
        if extra is None:
            requirements = package.get("dependencies", [])
        else:
            requirements = package.get("optional-dependencies", {}).get(extra, [])
        for requirement in requirements:
            pending.append((requirement["name"], None))
            pending.extend(
                (requirement["name"], e) for e in requirement.get("extra", [])
            )
    return {name for name, _ in visited} - {PROJECT}


def test_the_default_install_does_not_pull_logfire() -> None:
    # Negative test. Nothing here imports logfire (the binary build already
    # excludes it), yet the `pydantic-ai` meta-package installed it, and its
    # OpenTelemetry floor is what kept semgrep, and through it Zensical, from
    # resolving in the same lock.
    assert "logfire" not in _runtime_closure()


def test_the_closure_reaches_the_llm_providers_in_use() -> None:
    closure = _runtime_closure()
    assert {"openai", "anthropic", "google-genai"} <= closure, closure
