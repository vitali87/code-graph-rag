"""MCP startup must fail with the role-aware missing-key diagnostic (issue #1125),
not a wrapped provider error from the first tool call."""

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig
from codebase_rag.mcp import server as srv

_PROVIDER_KEY_ENV_VARS = (
    cs.ENV_ANTHROPIC_API_KEY,
    cs.ENV_OPENAI_API_KEY,
    cs.ENV_AZURE_API_KEY,
    cs.ENV_MINIMAX_API_KEY,
    cs.ENV_GOOGLE_API_KEY,
)


@contextlib.contextmanager
def _isolated_environment(**extra: str) -> Iterator[None]:
    """Isolate startup key validation from ambient provider keys.

    patch.dict merges into the real environment, so an ambient provider key
    would let ModelConfig.validate_api_key short-circuit and invert these
    assertions.
    """
    with patch.dict(os.environ, extra):
        for key in _PROVIDER_KEY_ENV_VARS:
            os.environ.pop(key, None)
        yield


def _remote_config_without_key() -> ModelConfig:
    return ModelConfig(provider="anthropic", model_id="claude-sonnet-5", api_key=None)


def _local_config() -> ModelConfig:
    return ModelConfig(provider="ollama", model_id="llama3.2", api_key=None)


class TestStartupKeyValidation:
    def test_remote_provider_without_key_fails_before_services(
        self, tmp_path: Path
    ) -> None:
        with (
            _isolated_environment(TARGET_REPO_PATH=str(tmp_path)),
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
            cypher_generator.assert_not_called()

    def test_cypher_role_is_validated_too(self, tmp_path: Path) -> None:
        with (
            _isolated_environment(TARGET_REPO_PATH=str(tmp_path)),
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
            ingestor.assert_not_called()
            cypher_generator.assert_not_called()

    def test_local_providers_keep_their_keyless_exemption(self, tmp_path: Path) -> None:
        with (
            _isolated_environment(TARGET_REPO_PATH=str(tmp_path)),
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
