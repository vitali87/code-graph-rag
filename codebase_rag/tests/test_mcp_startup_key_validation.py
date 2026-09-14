"""MCP startup must fail with the role-aware missing-key diagnostic (issue #1125),
not a wrapped provider error from the first tool call."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import API_KEY_INFO, ModelConfig
from codebase_rag.mcp import server as srv


def _remote_config_without_key() -> ModelConfig:
    return ModelConfig(provider="anthropic", model_id="claude-sonnet-5", api_key=None)


def _local_config() -> ModelConfig:
    return ModelConfig(provider="ollama", model_id="llama3.2", api_key=None)


# Every provider env var `validate_api_key` treats as an exemption, read from
# the one table the validator itself consults (#1913). Derived rather than
# hand-listed: a new provider in `API_KEY_INFO` is cleared here the day it is
# added, and the guard below proves each entry really does exempt.
_EXEMPTING_ENV_KEYS = tuple(info["env_var"] for info in API_KEY_INFO.values())


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


def test_the_exemption_list_covers_every_provider_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every variable the validator exempts on must be one this file clears.

    Behavioural rather than a source scan. The previous guard read
    `validate_api_key`'s source for `cs.ENV_*_API_KEY` literals; #1913 moved
    that lookup to `API_KEY_INFO`, the scan found nothing, and the guard fired
    on its own sanity check. Asking the validator what it does survives any
    further change in how it finds the name.

    Without this, a provider the validator exempts and this file does not clear
    re-introduces exactly the ambient-state dependency #1871 was about: on a
    machine holding that provider's key, every test below would take the
    exemption and assert nothing.
    """
    for provider, info in API_KEY_INFO.items():
        env_var = info["env_var"]
        monkeypatch.setenv(env_var, "ambient-key")
        try:
            ModelConfig(provider=provider, model_id="m").validate_api_key()
        except ValueError:  # pragma: no cover - not an exemption, nothing to clear
            continue
        finally:
            monkeypatch.delenv(env_var, raising=False)
        assert env_var in _EXEMPTING_ENV_KEYS, (
            f"{env_var} exempts {provider} from the missing-key branch but is "
            "not cleared by _isolated_env, so these tests depend on ambient state"
        )


def test_the_isolation_helper_clears_what_it_lists(tmp_path: Path) -> None:
    """And the list is actually applied. A correct list that the helper does
    not use would pass the guard above and still leak.
    """
    key = _EXEMPTING_ENV_KEYS[0]
    os.environ[key] = "ambient-key"
    try:
        with _isolated_env(tmp_path):
            assert key not in os.environ
    finally:
        os.environ.pop(key, None)


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
