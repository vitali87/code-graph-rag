"""What a default install pulls in, read from the lock rather than the manifest."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = "code-graph-rag"


def _runtime_closure(*extras: str) -> set[str]:
    # Environment markers are ignored, so this over-approximates across
    # platforms; that is the safe direction for a "never installed" check.
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    visited: set[tuple[str, str | None]] = set()
    pending: list[tuple[str, str | None]] = [(PROJECT, None)]
    pending.extend((PROJECT, extra) for extra in extras)
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


def test_the_default_install_does_not_pull_the_hugging_face_hub() -> None:
    # Negative test. Only transformers, in the semantic extra, downloads from
    # the Hub; as a base dependency the client also installed hf-xet, fsspec,
    # tqdm, filelock and packaging, none of which a default install imports.
    pulled = {"huggingface-hub", "hf-xet", "fsspec", "tqdm"} & _runtime_closure()
    assert not pulled, pulled


def test_the_default_install_does_not_pull_the_replaced_formats() -> None:
    # Negative test. cgr reads XML project files and writes workspace TOML
    # itself (rust/cgr_graph_rs/src/formats and codebase_rag/utils), so the
    # two packages it used for them are gone.
    pulled = {"defusedxml", "tomli-w"} & _runtime_closure()
    assert not pulled, pulled


def test_the_semantic_extra_still_pulls_the_hugging_face_hub() -> None:
    assert {"huggingface-hub", "hf-xet"} <= _runtime_closure("semantic")


def test_pyyaml_is_declared_rather_than_inherited() -> None:
    # The CLI imports codebase_rag/stack/manager.py, which imports yaml at
    # module level, so every command needs it. It used to arrive only as a
    # dependency of huggingface-hub.
    assert "pyyaml" in _declared_dependencies()
