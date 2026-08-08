"""`server.json` must not gate a keyless path behind a paid one (issue #1062).

The MCP manifest told clients an API key was mandatory and defaulted both
providers to anthropic, while `AppConfig` falls back to local ollama with
`DEFAULT_API_KEY` when no provider variables are set. A client reading the
manifest would refuse to install, or prompt for a paid key, on a configuration
that would have run keyless.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import LOCAL_PROVIDERS, AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "server.json"

PROVIDER_VARS = ("ORCHESTRATOR_PROVIDER", "CYPHER_PROVIDER")
MODEL_VARS = ("ORCHESTRATOR_MODEL", "CYPHER_MODEL")
API_KEY_VARS = ("ORCHESTRATOR_API_KEY", "CYPHER_API_KEY")


def _manifest_env() -> dict[str, dict[str, Any]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {
        var["name"]: var
        for package in manifest["packages"]
        for var in package.get("environmentVariables", [])
    }


@pytest.mark.parametrize("name", API_KEY_VARS)
def test_api_keys_are_not_required(name: str) -> None:
    var = _manifest_env()[name]

    assert not var.get("isRequired", False), (
        f"{name} is declared required, but the runtime falls back to local "
        "ollama with no key; an MCP client would prompt for a paid key on a "
        "configuration that runs free"
    )


@pytest.mark.parametrize("name", API_KEY_VARS)
def test_api_keys_stay_secret(name: str) -> None:
    assert _manifest_env()[name].get("isSecret", False), (
        f"{name} carries a credential and must stay marked secret"
    )


@pytest.mark.parametrize("name", PROVIDER_VARS)
def test_provider_defaults_match_the_runtime_fallback(name: str) -> None:
    declared = _manifest_env()[name].get("default")

    assert declared == cs.Provider.OLLAMA.value, (
        f"{name} defaults to {declared!r} in the manifest but the runtime "
        f"falls back to {cs.Provider.OLLAMA.value!r}"
    )


@pytest.mark.parametrize("name", MODEL_VARS)
def test_model_defaults_match_the_runtime_fallback(name: str) -> None:
    declared = _manifest_env()[name].get("default")

    assert declared == cs.DEFAULT_MODEL, (
        f"{name} defaults to {declared!r} in the manifest but the runtime "
        f"falls back to {cs.DEFAULT_MODEL!r}"
    )


def test_the_declared_default_provider_is_keyless() -> None:
    # The whole point: whatever the manifest defaults to must be a provider
    # `validate_api_key` exempts, or the keyless install cannot work.
    declared = _manifest_env()[PROVIDER_VARS[0]].get("default")

    assert declared in {provider.value for provider in LOCAL_PROVIDERS}, (
        f"{declared!r} is not in LOCAL_PROVIDERS, so it needs a key"
    )


def test_runtime_default_config_needs_no_user_supplied_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins the behaviour the manifest is being aligned to, so a change to
    # either side has to face the other.
    #
    # AppConfig reads settings case-insensitively and loads a local `.env`, so
    # clearing only the upper-case names would let `orchestrator_api_key` or a
    # developer's `.env` supply the very key this asserts is unnecessary.
    wanted = {
        f"{role}_{suffix}"
        for role in ("ORCHESTRATOR", "CYPHER")
        for suffix in ("PROVIDER", "MODEL", "API_KEY", "ENDPOINT")
    }
    for name in list(os.environ):
        if name.upper() in wanted:
            monkeypatch.delenv(name, raising=False)

    config = AppConfig(_env_file=None)
    model_config = config._get_default_orchestrator_config()

    assert model_config.provider == cs.Provider.OLLAMA
    assert model_config.api_key == cs.DEFAULT_API_KEY
    assert model_config.model_id == cs.DEFAULT_MODEL
