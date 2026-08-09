"""server.json must not mark an env var isRequired when the runtime can start without it."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_vars() -> dict[str, dict[str, object]]:
    manifest = json.loads((REPO_ROOT / "server.json").read_text())
    variables = manifest["packages"][0]["environmentVariables"]
    return {entry["name"]: entry for entry in variables}


class TestApiKeyEnvVarsNotUnconditionallyRequired:
    """isRequired can't express "required unless ollama"
    (test_local_providers_skip_validation), so it must be False or absent
    (the registry schema defaults isRequired to False)."""

    def test_orchestrator_api_key_not_marked_required(self) -> None:
        assert _env_vars()["ORCHESTRATOR_API_KEY"].get("isRequired", False) is False

    def test_cypher_api_key_not_marked_required(self) -> None:
        assert _env_vars()["CYPHER_API_KEY"].get("isRequired", False) is False
