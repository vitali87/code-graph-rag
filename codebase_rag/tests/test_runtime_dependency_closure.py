"""What a default install pulls in, read from the lock rather than the manifest."""

from __future__ import annotations

import re
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
    # excludes it), yet the `pydantic-ai` meta-package installed it.
    assert "logfire" not in _runtime_closure()


def test_the_closure_reaches_the_llm_providers_in_use() -> None:
    closure = _runtime_closure()
    assert {"openai", "anthropic", "google-genai"} <= closure, closure


def _declared_dependencies() -> set[str]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower()
        for requirement in config["project"]["dependencies"]
    }


def test_httpx_is_declared_directly() -> None:
    # The embedder, health checks, token counter and web search import httpx
    # themselves. Borrowed from the provider SDKs, it vanished from a fresh
    # install once google-genai 3 moved to httpx2, and `cgr` stopped starting.
    assert "httpx" in _declared_dependencies()
