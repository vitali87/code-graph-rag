"""MCP startup must fail with the role-aware missing-key diagnostic (issue #1125),
not a wrapped provider error from the first tool call."""

import inspect
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig
from codebase_rag.mcp import server as srv


def _remote_config_without_key() -> ModelConfig:
    return ModelConfig(provider="anthropic", model_id="claude-sonnet-5", api_key=None)


def _local_config() -> ModelConfig:
    return ModelConfig(provider="ollama", model_id="llama3.2", api_key=None)


# Every provider env var `validate_api_key` treats as an exemption. Kept here
# rather than inlined so a new provider added to that map and not to this list
# reopens the hole loudly, via the guard test below, instead of silently.
_EXEMPTING_ENV_KEYS = (
    cs.ENV_ANTHROPIC_API_KEY,
    cs.ENV_AZURE_API_KEY,
    cs.ENV_MINIMAX_API_KEY,
)


def _isolated_env(tmp_path: Path) -> Any:
    """Set the repo path AND clear every provider key the validator consults.

    `patch.dict` MERGES into the real environment, and `validate_api_key`
    returns early for a config whose provider env var is set. A developer
    machine holding a real `ANTHROPIC_API_KEY` therefore took that exemption,
    the missing-key branch was never reached, and the test asserted nothing --
    it passed or failed on ambient state rather than on the code (#1871).

    Cleared by rebuilding the mapping WITHOUT those keys and passing
    `clear=True`. PATH and the loader's own variables survive because they
    are carried over in `kept`, not because `clear` is off -- a bare
    `clear=False` merge is exactly the bug this helper exists to avoid.
    """
    # Built as an explicit replacement mapping rather than by mutating
    # os.environ after entry: `patch.dict` restores whatever it saved, so the
    # values have to be absent from the dict it is given, not deleted
    # afterwards. (Overriding the patcher's `__enter__` does NOT work --
    # `with` looks the method up on the type, not the instance, so the
    # override never runs and the keys survive.)
    kept = {
        key: value
        for key, value in os.environ.items()
        if key not in _EXEMPTING_ENV_KEYS
    }
    kept["TARGET_REPO_PATH"] = str(tmp_path)
    return patch.dict(os.environ, kept, clear=True)


def test_the_exemption_list_covers_every_provider_env_key() -> None:
    """The list above must not drift from the validator's own map.

    Without this, a provider added to `validate_api_key` and not here would
    re-introduce exactly the ambient-state dependency #1871 was about, and
    every test in this file would keep passing on a machine without that
    provider's key set.
    """
    validator = inspect.getsource(ModelConfig.validate_api_key)
    referenced = {
        name
        for name in dir(cs)
        if name.startswith("ENV_")
        and name.endswith("_API_KEY")
        and f"cs.{name}" in validator
    }
    assert referenced, (
        "fixture guard: found no provider env keys in validate_api_key, so "
        "this test cannot detect drift"
    )
    covered = {
        name for name in dir(cs) if getattr(cs, name, None) in _EXEMPTING_ENV_KEYS
    }
    assert referenced <= covered, (
        "validate_api_key exempts provider env keys this file does not clear, "
        f"so those tests depend on ambient state: {sorted(referenced - covered)}"
    )


class TestStartupKeyValidation:
    def test_remote_provider_without_key_fails_before_services(
        self, tmp_path: Path
    ) -> None:
        with (
            _isolated_env(tmp_path),
            patch.object(
                type(srv.settings),
                "active_orchestrator_config",
                property(lambda self: _remote_config_without_key()),
            ),
            patch.object(
                type(srv.settings),
                "active_cypher_config",
                property(lambda self: _local_config()),
            ),
            patch.object(srv, "MemgraphIngestor") as ingestor,
            patch.object(srv, "CypherGenerator") as cypher_generator,
        ):
            with pytest.raises(ValueError, match=cs.ModelRole.ORCHESTRATOR):
                srv.create_server()
            ingestor.assert_not_called()
            # The generator too, not just the ingestor: it is constructed
            # after the ingestor, and building it opens a connection to the
            # provider. Unmocked, this test reached a real Ollama endpoint
            # and failed on a machine that has none -- so it could not
            # distinguish "the key check fired" from "the key check did not
            # fire but the service happened to be reachable" (issue #1871).
            cypher_generator.assert_not_called()

    def test_cypher_role_is_validated_too(self, tmp_path: Path) -> None:
        with (
            _isolated_env(tmp_path),
            patch.object(
                type(srv.settings),
                "active_orchestrator_config",
                property(lambda self: _local_config()),
            ),
            patch.object(
                type(srv.settings),
                "active_cypher_config",
                property(lambda self: _remote_config_without_key()),
            ),
            patch.object(srv, "MemgraphIngestor") as ingestor,
            patch.object(srv, "CypherGenerator") as cypher_generator,
        ):
            with pytest.raises(ValueError, match=cs.ModelRole.CYPHER):
                srv.create_server()
            # Same reason as above. Here the ORCHESTRATOR is the local
            # provider, so an unmocked run reached the network while
            # validating a role this test is not even about.
            ingestor.assert_not_called()
            cypher_generator.assert_not_called()

    def test_local_providers_keep_their_keyless_exemption(self, tmp_path: Path) -> None:
        with (
            patch.dict(os.environ, {"TARGET_REPO_PATH": str(tmp_path)}),
            patch.object(
                type(srv.settings),
                "active_orchestrator_config",
                property(lambda self: _local_config()),
            ),
            patch.object(
                type(srv.settings),
                "active_cypher_config",
                property(lambda self: _local_config()),
            ),
            patch.object(srv, "MemgraphIngestor"),
            patch.object(srv, "CypherGenerator"),
            patch.object(srv, "create_mcp_tools_registry"),
        ):
            server, _ = srv.create_server()
            assert server is not None
