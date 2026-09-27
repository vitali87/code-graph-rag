"""Optional authentication for the bundled Memgraph and Qdrant stack.

The compose file lists the container variables without values, so each is set
only when `docker compose` runs with it; `cgr daemon up` fills them from the
settings the app itself logs in with.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from loguru import logger

from codebase_rag.config import settings
from codebase_rag.stack import constants as cs
from codebase_rag.stack import health
from codebase_rag.stack.manager import StackManager

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "codebase_rag" / "docker-compose.yaml"

STALE_COMPOSE = (
    "services:\n"
    "  memgraph:\n"
    '    ports: ["127.0.0.1:7687:7687"]\n'
    "  qdrant:\n"
    '    ports: ["127.0.0.1:6333:6333"]\n'
)


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", "cgr")
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", "s3cret")
    monkeypatch.setattr(settings, "QDRANT_API_KEY", "qdrant-key")


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", None)
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", None)
    monkeypatch.setattr(settings, "QDRANT_API_KEY", None)


def _manager(tmp_path: Path) -> StackManager:
    return StackManager(home=tmp_path / "cgr-home", package_compose=COMPOSE_PATH)


def _warnings_from(action: Callable[[], Path]) -> list[str]:
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        action()
    finally:
        logger.remove(sink_id)
    return messages


def test_compose_file_passes_auth_variables_through_without_values() -> None:
    # A value (even an empty `${VAR:-}`) would always set the variable; only a
    # bare name leaves it unset in the container when nothing provides it.
    services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]

    assert cs.ENV_MEMGRAPH_USER in services[cs.SERVICE_MEMGRAPH]["environment"]
    assert cs.ENV_MEMGRAPH_PASSWORD in services[cs.SERVICE_MEMGRAPH]["environment"]
    assert cs.ENV_QDRANT_API_KEY in services[cs.SERVICE_QDRANT]["environment"]


@pytest.mark.usefixtures("credentials")
def test_compose_env_carries_configured_credentials(tmp_path: Path) -> None:
    env = _manager(tmp_path)._compose_env()

    assert env[cs.ENV_MEMGRAPH_USER] == "cgr"
    assert env[cs.ENV_MEMGRAPH_PASSWORD] == "s3cret"
    assert env[cs.ENV_QDRANT_API_KEY] == "qdrant-key"


@pytest.mark.usefixtures("no_credentials")
def test_compose_env_drops_inherited_auth_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in cs.STACK_AUTH_ENV_VARS:
        monkeypatch.setenv(name, "stray")
    monkeypatch.setenv("CGR_STACK_BIND_HOST", "127.0.0.1")

    env = _manager(tmp_path)._compose_env()

    assert not set(cs.STACK_AUTH_ENV_VARS) & env.keys()
    assert env["CGR_STACK_BIND_HOST"] == "127.0.0.1"


@pytest.mark.parametrize(
    ("username", "password"),
    [("cgr", None), (None, "s3cret"), ("cgr", "   "), ("  ", "s3cret")],
)
def test_half_set_memgraph_credentials_are_not_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    username: str | None,
    password: str | None,
) -> None:
    # Memgraph turns no authentication on for a username without a password.
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", username)
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", password)
    monkeypatch.setattr(settings, "QDRANT_API_KEY", None)

    mgr = _manager(tmp_path)

    assert mgr.memgraph_credentials is None
    assert cs.ENV_MEMGRAPH_USER not in mgr._compose_env()


def test_memgraph_credentials_are_stripped_like_the_ingestor_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", " cgr ")
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", " s3cret\n")

    assert _manager(tmp_path).memgraph_credentials == ("cgr", "s3cret")


@pytest.mark.usefixtures("credentials")
def test_up_runs_compose_with_the_credentials(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as run,
    ):
        mgr.up()

    env = run.call_args.kwargs["env"]
    assert env[cs.ENV_MEMGRAPH_USER] == "cgr"
    assert env[cs.ENV_QDRANT_API_KEY] == "qdrant-key"


@pytest.mark.usefixtures("credentials")
def test_health_probes_log_in_to_memgraph(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with (
        patch(
            "codebase_rag.stack.manager.wait_for_memgraph", return_value=True
        ) as probe,
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
    ):
        mgr.wait_healthy(timeout=0.1)
        mgr.status()

    for call in probe.call_args_list:
        assert call.kwargs["credentials"] == ("cgr", "s3cret")


def test_bolt_probe_passes_the_login_to_mgclient() -> None:
    with patch.object(health.mgclient, "connect") as connect:
        assert health._bolt_reachable("localhost", 7687, ("cgr", "s3cret"))

    connect.assert_called_once_with(
        host="localhost", port=7687, username="cgr", password="s3cret"
    )


def test_bolt_probe_without_a_login_connects_anonymously() -> None:
    with patch.object(health.mgclient, "connect") as connect:
        assert health._bolt_reachable("localhost", 7687)

    connect.assert_called_once_with(host="localhost", port=7687)


@pytest.mark.usefixtures("credentials")
def test_stale_compose_file_warns_that_auth_is_not_wired(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.ensure_home()
    mgr.compose_file.write_text(STALE_COMPOSE, encoding="utf-8")

    messages = _warnings_from(mgr.ensure_compose_file)

    warning = next(m for m in messages if "WITHOUT authentication" in m)
    for name in cs.STACK_AUTH_ENV_VARS:
        assert name in warning


@pytest.mark.usefixtures("credentials")
def test_current_compose_file_does_not_warn(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.ensure_compose_file()

    messages = _warnings_from(mgr.ensure_compose_file)

    assert not any("WITHOUT authentication" in m for m in messages)


@pytest.mark.usefixtures("credentials")
def test_map_form_environment_counts_as_wired(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.ensure_home()
    mgr.compose_file.write_text(
        "services:\n"
        "  memgraph:\n"
        "    environment:\n"
        "      MEMGRAPH_USER: cgr\n"
        "      MEMGRAPH_PASSWORD: s3cret\n"
        "  qdrant:\n"
        "    environment:\n"
        "      QDRANT__SERVICE__API_KEY: qdrant-key\n",
        encoding="utf-8",
    )

    messages = _warnings_from(mgr.ensure_compose_file)

    assert not any("WITHOUT authentication" in m for m in messages)


@pytest.mark.usefixtures("no_credentials")
def test_stale_compose_file_without_credentials_does_not_warn(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    mgr.ensure_home()
    mgr.compose_file.write_text(STALE_COMPOSE, encoding="utf-8")

    messages = _warnings_from(mgr.ensure_compose_file)

    assert not any("WITHOUT authentication" in m for m in messages)
