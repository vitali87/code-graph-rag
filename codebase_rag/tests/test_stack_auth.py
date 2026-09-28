"""Optional authentication for the bundled Memgraph and Qdrant stack.

The compose file lists the container variables without values, so each is set
only when `docker compose` runs with it; `cgr daemon up` fills them from the
settings the app itself logs in with, and refuses to start when Compose would
resolve something else.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
import urllib.error
from collections.abc import Callable, Iterator
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from loguru import logger

from codebase_rag.config import settings
from codebase_rag.stack import constants as cs
from codebase_rag.stack import health
from codebase_rag.stack.manager import StackError, StackManager

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "codebase_rag" / "docker-compose.yaml"

MATCHING_ENV = {
    cs.SERVICE_MEMGRAPH: {
        cs.ENV_MEMGRAPH_USER: "cgr",
        cs.ENV_MEMGRAPH_PASSWORD: "s3cret",
    },
    cs.SERVICE_QDRANT: {cs.ENV_QDRANT_API_KEY: "qdrant-key"},
}
UNRESOLVED_ENV = {
    cs.SERVICE_MEMGRAPH: {cs.ENV_MEMGRAPH_USER: None, cs.ENV_MEMGRAPH_PASSWORD: None},
    cs.SERVICE_QDRANT: {cs.ENV_QDRANT_API_KEY: None},
}


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


def _warnings_from[T](action: Callable[[], T]) -> list[str]:
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        action()
    finally:
        logger.remove(sink_id)
    return messages


def _compose_config(
    environments: dict[str, dict[str, str | None]],
) -> subprocess.CompletedProcess[str]:
    """What `docker compose config --format json` prints for these services."""
    services = {service: {"environment": env} for service, env in environments.items()}
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"services": services}), stderr=""
    )


def _verify_with(
    mgr: StackManager, result: subprocess.CompletedProcess[str]
) -> MagicMock:
    with patch("codebase_rag.stack.manager.subprocess.run", return_value=result) as run:
        mgr._verify_resolved_auth()
    return run


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
def test_up_verifies_then_starts_compose_with_the_credentials(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            side_effect=[
                _compose_config(MATCHING_ENV),
                subprocess.CompletedProcess(args=[], returncode=0),
            ],
        ) as run,
    ):
        mgr.up()

    config_call, up_call = run.call_args_list
    assert "config" in config_call.args[0]
    assert up_call.args[0][-2:] == ["up", "-d"]
    for call in (config_call, up_call):
        assert call.kwargs["env"][cs.ENV_MEMGRAPH_USER] == "cgr"
        assert call.kwargs["env"][cs.ENV_QDRANT_API_KEY] == "qdrant-key"


@pytest.mark.usefixtures("credentials")
def test_up_does_not_start_when_compose_resolves_other_credentials(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            return_value=_compose_config(UNRESOLVED_ENV),
        ) as run,
    ):
        with pytest.raises(StackError):
            mgr.up()

    assert run.call_count == 1


@pytest.mark.usefixtures("credentials")
def test_verify_accepts_compose_resolving_the_settings(tmp_path: Path) -> None:
    _verify_with(_manager(tmp_path), _compose_config(MATCHING_ENV))


@pytest.mark.usefixtures("no_credentials")
def test_verify_accepts_unresolved_variables_without_credentials(
    tmp_path: Path,
) -> None:
    _verify_with(_manager(tmp_path), _compose_config(UNRESOLVED_ENV))


@pytest.mark.usefixtures("credentials")
def test_verify_refuses_a_compose_file_that_drops_the_credentials(
    tmp_path: Path,
) -> None:
    # A file rendered before credential support declares no `environment`.
    stale = _compose_config({cs.SERVICE_MEMGRAPH: {}, cs.SERVICE_QDRANT: {}})

    mgr = _manager(tmp_path)
    with pytest.raises(StackError) as exc:
        _verify_with(mgr, stale)

    for name in cs.STACK_AUTH_ENV_VARS:
        assert name in str(exc.value)


@pytest.mark.usefixtures("credentials")
def test_verify_refuses_a_value_written_into_the_compose_file(
    tmp_path: Path,
) -> None:
    # An empty key written into the file wins over the settings, and Qdrant
    # treats an empty key as none at all.
    hardcoded = _compose_config(
        {
            cs.SERVICE_MEMGRAPH: MATCHING_ENV[cs.SERVICE_MEMGRAPH],
            cs.SERVICE_QDRANT: {cs.ENV_QDRANT_API_KEY: ""},
        }
    )

    mgr = _manager(tmp_path)
    with pytest.raises(StackError) as exc:
        _verify_with(mgr, hardcoded)

    assert f"{cs.SERVICE_QDRANT}: {cs.ENV_QDRANT_API_KEY}" in str(exc.value)
    assert f"{cs.SERVICE_MEMGRAPH}: {cs.ENV_MEMGRAPH_USER}" not in str(exc.value)


@pytest.mark.usefixtures("no_credentials")
def test_verify_refuses_a_key_from_an_env_file_beside_the_compose_file(
    tmp_path: Path,
) -> None:
    # Compose reads that .env for a variable missing from its environment.
    from_env_file = _compose_config(
        {
            cs.SERVICE_MEMGRAPH: UNRESOLVED_ENV[cs.SERVICE_MEMGRAPH],
            cs.SERVICE_QDRANT: {cs.ENV_QDRANT_API_KEY: "key-from-env-file"},
        }
    )

    mgr = _manager(tmp_path)
    with pytest.raises(StackError) as exc:
        _verify_with(mgr, from_env_file)

    assert cs.ENV_QDRANT_API_KEY in str(exc.value)
    assert "key-from-env-file" not in str(exc.value)


FAILED_CONFIG = subprocess.CompletedProcess(
    args=[], returncode=1, stdout="", stderr="unknown flag: --format"
)


@pytest.mark.parametrize("configured", ["credentials", "no_credentials"])
def test_verify_refuses_to_start_when_compose_config_fails(
    tmp_path: Path, request: pytest.FixtureRequest, configured: str
) -> None:
    # Unchecked, the stack could start open, or with a key from an .env file
    # beside the compose file that the app does not have.
    request.getfixturevalue(configured)

    mgr = _manager(tmp_path)
    with pytest.raises(StackError) as exc:
        _verify_with(mgr, FAILED_CONFIG)

    assert "unknown flag: --format" in str(exc.value)


@pytest.mark.usefixtures("credentials")
def test_verify_falls_back_to_plain_config_without_the_format_flag(
    tmp_path: Path,
) -> None:
    services = {service: {"environment": env} for service, env in MATCHING_ENV.items()}
    plain = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=yaml.safe_dump({"services": services}), stderr=""
    )
    mgr = _manager(tmp_path)
    with patch(
        "codebase_rag.stack.manager.subprocess.run", side_effect=[FAILED_CONFIG, plain]
    ) as run:
        mgr._verify_resolved_auth()

    json_call, plain_call = run.call_args_list
    assert "--format" in json_call.args[0]
    assert plain_call.args[0][-1] == "config"


@pytest.mark.usefixtures("credentials")
def test_verify_reads_yaml_printed_despite_the_json_flag(tmp_path: Path) -> None:
    services = {service: {"environment": env} for service, env in MATCHING_ENV.items()}
    as_yaml = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=yaml.safe_dump({"services": services}), stderr=""
    )

    _verify_with(_manager(tmp_path), as_yaml)


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


def test_memgraph_anonymous_probe_sends_no_login() -> None:
    with patch.object(health.mgclient, "connect") as connect:
        assert health.memgraph_accepts_anonymous("localhost", 7687)

    connect.assert_called_once_with(host="localhost", port=7687)


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value.status = 200
    return response


def test_qdrant_anonymous_probe_reads_a_data_endpoint() -> None:
    with patch.object(
        health._DIRECT_OPENER, "open", return_value=_ok_response()
    ) as open_url:
        assert health.qdrant_accepts_anonymous(6333)

    request = open_url.call_args.args[0]
    assert request.full_url.endswith(cs.QDRANT_DATA_PROBE_PATH)
    assert not request.has_header(cs.QDRANT_API_KEY_HEADER.capitalize())


def test_qdrant_key_probe_sends_the_configured_key() -> None:
    with patch.object(
        health._DIRECT_OPENER, "open", return_value=_ok_response()
    ) as open_url:
        assert health.qdrant_accepts_key(6333, "qdrant-key")

    request = open_url.call_args.args[0]
    assert request.get_header(cs.QDRANT_API_KEY_HEADER.capitalize()) == "qdrant-key"


def test_qdrant_anonymous_probe_is_false_when_the_key_is_required() -> None:
    unauthorized = urllib.error.HTTPError(
        "http://127.0.0.1:6333/collections", 401, "Unauthorized", Message(), None
    )
    with patch.object(health._DIRECT_OPENER, "open", side_effect=unauthorized):
        assert not health.qdrant_accepts_anonymous(6333)


@pytest.fixture
def local_qdrant_port() -> Iterator[int]:
    """A loopback HTTP server that answers 200, standing in for Qdrant."""

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_qdrant_key_probe_bypasses_an_http_proxy(
    local_qdrant_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Through the proxy, the api-key header would reach it; the proxy here is
    # a closed port, so the probe only succeeds if it connects directly.
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    assert health.qdrant_accepts_key(local_qdrant_port, "qdrant-key")


def _ensure_running_on_a_healthy_stack(
    mgr: StackManager,
    memgraph_open: bool,
    qdrant_open: bool,
    qdrant_key_accepted: bool = True,
) -> tuple[list[str], MagicMock, MagicMock]:
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous",
            return_value=memgraph_open,
        ) as memgraph_probe,
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_anonymous",
            return_value=qdrant_open,
        ) as qdrant_probe,
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_key",
            return_value=qdrant_key_accepted,
        ),
    ):
        messages = _warnings_from(mgr.ensure_running)
    return messages, memgraph_probe, qdrant_probe


@pytest.mark.usefixtures("credentials")
def test_running_stack_that_accepts_anonymous_access_is_flagged(
    tmp_path: Path,
) -> None:
    # A stack created before the credentials were set stays open, and its
    # health checks pass either way.
    messages, _, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path), memgraph_open=True, qdrant_open=True
    )

    warning = next(m for m in messages if "still accept" in m)
    assert cs.SERVICE_MEMGRAPH in warning
    assert cs.SERVICE_QDRANT in warning


@pytest.mark.usefixtures("credentials")
def test_running_stack_that_requires_the_credentials_is_not_flagged(
    tmp_path: Path,
) -> None:
    messages, _, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path), memgraph_open=False, qdrant_open=False
    )

    assert not any("still accept" in m for m in messages)


@pytest.mark.usefixtures("no_credentials")
def test_running_stack_without_credentials_is_not_probed(tmp_path: Path) -> None:
    messages, memgraph_probe, qdrant_probe = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path), memgraph_open=True, qdrant_open=True
    )

    memgraph_probe.assert_not_called()
    qdrant_probe.assert_not_called()
    assert not any("still accept" in m for m in messages)


@pytest.mark.usefixtures("credentials")
def test_running_qdrant_with_an_earlier_key_is_flagged(tmp_path: Path) -> None:
    # Anonymous requests are rejected, so only trying the configured key shows
    # that the app's requests would be rejected too.
    messages, _, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path),
        memgraph_open=False,
        qdrant_open=False,
        qdrant_key_accepted=False,
    )

    assert any("rejects the configured QDRANT_API_KEY" in m for m in messages)
    assert not any("still accept" in m for m in messages)
