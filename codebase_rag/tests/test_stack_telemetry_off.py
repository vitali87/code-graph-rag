"""Issue #2675: the packaged stack starts with vendor telemetry off.

`cgr daemon up` ran Memgraph with no flags and Qdrant with only its API key,
and both images default to reporting usage telemetry: Memgraph sends the
host's CPU and memory profile and the graph's vertex and edge counts, Qdrant
reports to telemetry.qdrant.io on startup. Nothing told the user.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codebase_rag.stack import constants as stack_cs
from codebase_rag.stack.manager import StackManager
from codebase_rag.types_defs import JsonValue

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "codebase_rag" / "docker-compose.yaml"
SECURITY_DOC = REPO_ROOT / "docs" / "architecture" / "security.md"


def _service(compose_file: Path, name: str) -> dict[str, JsonValue]:
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    return compose["services"][name]


def _environment(service: dict[str, JsonValue]) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return {str(k): str(v) for k, v in environment.items()}
    assert isinstance(environment, list)
    pairs = (str(entry).partition("=") for entry in environment)
    return {name: value for name, _eq, value in pairs}


def _rendered(tmp_path: Path) -> Path:
    return StackManager(
        home=tmp_path, package_compose=COMPOSE_PATH
    ).ensure_compose_file()


def test_memgraph_starts_with_telemetry_off(tmp_path: Path) -> None:
    memgraph = _service(_rendered(tmp_path), stack_cs.SERVICE_MEMGRAPH)

    command = memgraph.get("command")
    assert isinstance(command, list)
    assert "--telemetry-enabled=false" in command


def test_qdrant_starts_with_telemetry_off(tmp_path: Path) -> None:
    qdrant = _service(_rendered(tmp_path), stack_cs.SERVICE_QDRANT)

    assert _environment(qdrant).get("QDRANT__TELEMETRY_DISABLED") == "true"


def test_the_security_guide_says_so() -> None:
    text = SECURITY_DOC.read_text(encoding="utf-8")

    assert "--telemetry-enabled=false" in text
    assert "QDRANT__TELEMETRY_DISABLED" in text


# Negative: what must not change.


def test_the_credential_variables_are_still_passed_through(tmp_path: Path) -> None:
    rendered = _rendered(tmp_path)

    assert {"MEMGRAPH_USER", "MEMGRAPH_PASSWORD"} <= set(
        _environment(_service(rendered, stack_cs.SERVICE_MEMGRAPH))
    )
    assert "QDRANT__SERVICE__API_KEY" in _environment(
        _service(rendered, stack_cs.SERVICE_QDRANT)
    )


def test_the_api_key_is_still_unset_unless_the_environment_sets_it(
    tmp_path: Path,
) -> None:
    qdrant = _environment(_service(_rendered(tmp_path), stack_cs.SERVICE_QDRANT))

    assert qdrant["QDRANT__SERVICE__API_KEY"] == ""
