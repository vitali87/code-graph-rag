"""Optional authentication for the bundled Memgraph and Qdrant stack.

The compose file lists the container variables without values, so each is set
only when `docker compose` runs with it; `cgr daemon up` fills them from the
settings the app itself logs in with, and refuses to start when Compose would
resolve something else.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import subprocess
import threading
import urllib.error
from collections.abc import Callable, Iterator
from email.message import Message
from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock, patch

import pytest
import typer
import yaml
from click.testing import CliRunner
from loguru import logger

from codebase_rag.config import settings
from codebase_rag.stack import cli as stack_cli
from codebase_rag.stack import constants as cs
from codebase_rag.stack import health
from codebase_rag.stack import manager as manager_module
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


BUNDLED_QDRANT_URL = "http://localhost:6333"
# Kept before the fixture below stands in for it.
REAL_CONTAINER_IDS = manager_module._container_ids


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", "cgr")
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", "s3cret")
    monkeypatch.setattr(settings, "QDRANT_API_KEY", "qdrant-key")
    monkeypatch.setattr(settings, "QDRANT_URL", BUNDLED_QDRANT_URL)
    monkeypatch.delenv(cs.COMPOSE_BIND_HOST_VAR, raising=False)


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", None)
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", None)
    monkeypatch.setattr(settings, "QDRANT_API_KEY", None)


@pytest.fixture(autouse=True)
def _nothing_running_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # `up` looks for a service already running open before it starts, and no
    # unit test reaches a real Memgraph or Qdrant; a test that needs another
    # answer patches the probe itself.
    monkeypatch.setattr(manager_module, "memgraph_accepts_anonymous", _closed)
    monkeypatch.setattr(manager_module, "qdrant_accepts_anonymous", _closed)
    monkeypatch.setattr(manager_module, "memgraph_anonymous_access", _refused)
    monkeypatch.setattr(manager_module, "qdrant_anonymous_access", _refused)
    # Nor a Docker engine: no service has a running container before a start.
    monkeypatch.setattr(manager_module, "_container_ids", _no_containers)


def _closed(*_: str | int, **__: str) -> bool:
    return False


def _refused(*_: str | int, **__: str) -> cs.AnonymousAccess:
    return cs.AnonymousAccess.REFUSED


def _no_containers(*_: list[str] | dict[str, str]) -> str:
    return ""


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


class _PortEntry(TypedDict, total=False):
    mode: str
    host_ip: str
    target: int
    published: str


class _ResolvedService(TypedDict, total=False):
    environment: dict[str, str | None]
    ports: list[_PortEntry]


def _compose_config(
    environments: dict[str, dict[str, str | None]],
    ports: dict[str, list[_PortEntry]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """What `docker compose config --format json` prints for these services."""
    services: dict[str, _ResolvedService] = {
        service: {"environment": env} for service, env in environments.items()
    }
    for service, entries in (ports or {}).items():
        services[service]["ports"] = entries
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"services": services}), stderr=""
    )


def _verify_with(
    mgr: StackManager, result: subprocess.CompletedProcess[str]
) -> MagicMock:
    with patch("codebase_rag.stack.manager.subprocess.run", return_value=result) as run:
        mgr._verify_resolved_auth(mgr._resolved_config())
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
        mgr._verify_resolved_auth(mgr._resolved_config())

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
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous", return_value=False
        ),
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_anonymous", return_value=False
        ),
    ):
        mgr.wait_healthy(timeout=0.1)
        mgr.status()

    for call in probe.call_args_list:
        assert call.kwargs["credentials"] == ("cgr", "s3cret")


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("memgraph_open", "qdrant_open", "open_services"),
    [(True, False, "memgraph"), (False, True, "qdrant")],
)
def test_a_started_stack_that_does_not_enforce_the_credentials_is_refused(
    tmp_path: Path, memgraph_open: bool, qdrant_open: bool, open_services: str
) -> None:
    # `up` and `restart` end in wait_healthy: a ready service is checked for
    # the credentials too, whether or not Compose recreated its container,
    # and an open service just started is stopped rather than left running.
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous",
            return_value=memgraph_open,
        ),
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_anonymous",
            return_value=qdrant_open,
        ),
        patch.object(mgr, "stop") as stop,
        pytest.raises(StackError, match=f"without them: {open_services}\\."),
    ):
        mgr.wait_healthy(timeout=0.1)

    stop.assert_called_once_with(open_services)


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    "stop_failure",
    [
        StackError("daemon gone"),
        subprocess.TimeoutExpired(["docker", "compose", "stop"], 120),
        FileNotFoundError("docker"),
    ],
)
def test_a_failed_stop_does_not_hide_the_credential_error(
    tmp_path: Path, stop_failure: Exception
) -> None:
    mgr = _manager(tmp_path)
    refusals: list[str] = []
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous", return_value=True
        ),
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_anonymous", return_value=False
        ),
        patch.object(mgr, "stop", side_effect=stop_failure),
    ):
        messages = _warnings_from(
            lambda: refusals.append(_refusal_of(lambda: mgr.wait_healthy(0.1)))
        )

    assert "still accept" in refusals[0]
    assert any(str(stop_failure) in m and "cgr daemon down" in m for m in messages)


def _refusal_of(action: Callable[[], None]) -> str:
    try:
        action()
    except StackError as refused:
        return str(refused)
    raise AssertionError("no StackError")


@pytest.mark.usefixtures("credentials")
def test_a_stack_found_running_open_is_refused_but_not_stopped(
    tmp_path: Path,
) -> None:
    # This invocation did not start it, and other clients may be using it.
    mgr = _manager(tmp_path)
    with patch.object(mgr, "stop") as stop:
        error, _, _, _ = _ensure_running_on_a_healthy_stack(
            mgr, memgraph_open=True, qdrant_open=False
        )

    assert error is not None
    assert "still accept" in error
    stop.assert_not_called()


@contextlib.contextmanager
def _left_running(mgr: StackManager, memgraph_open: bool) -> Iterator[MagicMock]:
    """A Memgraph open or not and a protected Qdrant; yields the patched `stop`."""
    memgraph_access = (
        cs.AnonymousAccess.ALLOWED if memgraph_open else cs.AnonymousAccess.REFUSED
    )
    with (
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous",
            return_value=memgraph_open,
        ),
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_anonymous", return_value=False
        ),
        patch(
            "codebase_rag.stack.manager.memgraph_anonymous_access",
            return_value=memgraph_access,
        ),
        patch.object(mgr, "stop") as stop,
    ):
        yield stop


def _failure_of(action: Callable[[], None]) -> tuple[BaseException, list[str]]:
    """The exception an action ends in, and the warnings it logs on the way."""
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        action()
    except (StackError, subprocess.TimeoutExpired, KeyboardInterrupt) as failure:
        return failure, messages
    finally:
        logger.remove(sink_id)
    raise AssertionError("the action did not fail")


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("qdrant_ready", "failure"),
    [(False, StackError), (KeyboardInterrupt(), KeyboardInterrupt)],
)
@pytest.mark.parametrize(
    ("memgraph_open", "stopped"),
    [(True, [(cs.SERVICE_MEMGRAPH,)]), (False, [])],
)
def test_a_start_whose_health_wait_fails_stops_only_an_open_service(
    tmp_path: Path,
    qdrant_ready: bool | KeyboardInterrupt,
    failure: type[BaseException],
    memgraph_open: bool,
    stopped: list[tuple[str, ...]],
) -> None:
    # Such a start never reaches the credential check after the wait. A
    # protected service that is merely slow to start is left running.
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", side_effect=[qdrant_ready]),
        _left_running(mgr, memgraph_open) as stop,
    ):
        error, messages = _failure_of(lambda: mgr.wait_healthy(0))

    assert isinstance(error, failure)
    assert [c.args for c in stop.call_args_list] == stopped
    warned = any("did not finish" in m and cs.SERVICE_MEMGRAPH in m for m in messages)
    assert warned is memgraph_open


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("compose_up", "failure"),
    [
        (
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="port is already allocated"
            ),
            StackError,
        ),
        (
            subprocess.TimeoutExpired(["docker", "compose", "up", "-d"], 120),
            subprocess.TimeoutExpired,
        ),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ],
)
@pytest.mark.parametrize(
    ("memgraph_open", "stopped"),
    [(True, [(cs.SERVICE_MEMGRAPH,)]), (False, [])],
)
def test_up_that_fails_stops_only_an_open_service_it_started(
    tmp_path: Path,
    compose_up: subprocess.CompletedProcess[str] | BaseException,
    failure: type[BaseException],
    memgraph_open: bool,
    stopped: list[tuple[str, ...]],
) -> None:
    # `up -d` can start some containers before it fails or is cut short.
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            side_effect=[_compose_config(MATCHING_ENV), compose_up],
        ),
        # Closed before `up -d`, so a Memgraph open after it is this start's.
        patch(
            "codebase_rag.stack.manager.memgraph_anonymous_access",
            return_value=(
                cs.AnonymousAccess.ALLOWED
                if memgraph_open
                else cs.AnonymousAccess.REFUSED
            ),
        ),
        patch.object(mgr, "stop") as stop,
    ):
        error, _ = _failure_of(mgr.up)

    assert isinstance(error, failure)
    assert [c.args for c in stop.call_args_list] == stopped


@pytest.mark.usefixtures("credentials")
def test_up_refuses_a_stack_already_running_open_and_leaves_it_running(
    tmp_path: Path,
) -> None:
    # This start did not start it, and other clients may be using it. Refused
    # here, nothing open after `up -d` can be another invocation's.
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            return_value=_compose_config(MATCHING_ENV),
        ) as run,
        _left_running(mgr, memgraph_open=True) as stop,
        pytest.raises(StackError, match="without them: memgraph\\."),
    ):
        mgr.up()

    assert all(c.args[0][-2:] != ["up", "-d"] for c in run.call_args_list)
    stop.assert_not_called()


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("qdrant_answers", "stopped"),
    [
        (
            [cs.AnonymousAccess.NO_ANSWER, cs.AnonymousAccess.ALLOWED],
            [(cs.SERVICE_QDRANT,)],
        ),
        ([cs.AnonymousAccess.NO_ANSWER, cs.AnonymousAccess.REFUSED], []),
    ],
)
def test_a_failed_start_asks_again_a_service_that_is_still_initialising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qdrant_answers: list[cs.AnonymousAccess],
    stopped: list[tuple[str, ...]],
) -> None:
    # A container `up -d` started can answer only after the first check; it
    # is stopped if it then turns out open, and left running if protected.
    monkeypatch.setattr(cs, "DEFAULT_HEALTH_INTERVAL_S", 0)
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=False),
        patch(
            "codebase_rag.stack.manager.qdrant_anonymous_access",
            side_effect=qdrant_answers,
        ) as qdrant_probe,
        patch.object(mgr, "stop") as stop,
    ):
        error, _ = _failure_of(lambda: mgr.wait_healthy(0))

    assert isinstance(error, StackError)
    assert qdrant_probe.call_count == len(qdrant_answers)
    assert [c.args for c in stop.call_args_list] == stopped


@pytest.mark.usefixtures("credentials")
def test_a_failed_start_stops_asking_a_silent_service_after_its_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing that does not answer is exposed; it is left running, and the
    # failure is reported once the check times out.
    monkeypatch.setattr(cs, "STARTED_SERVICES_CHECK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(cs, "DEFAULT_HEALTH_INTERVAL_S", 0.01)
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=False),
        patch(
            "codebase_rag.stack.manager.qdrant_anonymous_access",
            return_value=cs.AnonymousAccess.NO_ANSWER,
        ) as qdrant_probe,
        patch.object(mgr, "stop") as stop,
        pytest.raises(StackError, match="qdrant did not become healthy"),
    ):
        mgr.wait_healthy(0)

    assert qdrant_probe.call_count > 1
    stop.assert_not_called()


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("running_after", "stopped", "left_running"),
    [
        # The container that was running before `up -d`, left as it was.
        ({"memgraph": "a1b2", "qdrant": ""}, [], True),
        # Recreated or started by this start.
        ({"memgraph": "c3d4", "qdrant": ""}, [(cs.SERVICE_MEMGRAPH,)], False),
        # Compose cannot tell: stopped rather than left open.
        (None, [(cs.SERVICE_MEMGRAPH,)], False),
    ],
)
def test_a_failed_start_stops_only_a_container_it_created_or_started(
    tmp_path: Path,
    running_after: dict[str, str] | None,
    stopped: list[tuple[str, ...]],
    left_running: bool,
) -> None:
    # A Memgraph that did not answer yet when `up` checked, found open after
    # `up -d` failed, is this start's to stop only if its container changed.
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            side_effect=[
                _compose_config(MATCHING_ENV),
                subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="port is allocated"
                ),
            ],
        ),
        patch.object(
            mgr,
            "_running_containers",
            side_effect=[{"memgraph": "a1b2", "qdrant": ""}, running_after],
        ),
        patch(
            "codebase_rag.stack.manager.memgraph_anonymous_access",
            return_value=cs.AnonymousAccess.ALLOWED,
        ),
        patch.object(mgr, "stop") as stop,
    ):
        error, messages = _failure_of(mgr.up)

    assert isinstance(error, StackError)
    assert [c.args for c in stop.call_args_list] == stopped
    warned = any("already running before this start" in m for m in messages)
    assert warned is left_running


@pytest.mark.usefixtures("credentials")
def test_a_started_service_found_open_but_running_before_is_refused_not_stopped(
    tmp_path: Path,
) -> None:
    # The post-start check follows the same ownership as the failed-start one.
    mgr = _manager(tmp_path)
    mgr._containers_before_start = {"memgraph": "a1b2", "qdrant": ""}
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch.object(
            mgr, "_running_containers", return_value={"memgraph": "a1b2", "qdrant": ""}
        ),
        _left_running(mgr, memgraph_open=True) as stop,
        pytest.raises(StackError, match="without them: memgraph\\."),
    ):
        mgr.wait_healthy(0)

    stop.assert_not_called()


def test_running_containers_asks_compose_for_each_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "_container_ids", REAL_CONTAINER_IDS)
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", "cgr")
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", "s3cret")
    monkeypatch.setattr(settings, "QDRANT_API_KEY", None)
    mgr = _manager(tmp_path)
    with patch(
        "codebase_rag.stack.manager.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="a1b2\n"
        ),
    ) as run:
        containers = mgr._running_containers()

    assert containers == {cs.SERVICE_MEMGRAPH: "a1b2"}
    assert run.call_args.args[0][-5:] == [
        "ps",
        "--quiet",
        "--status",
        "running",
        cs.SERVICE_MEMGRAPH,
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no"),
        subprocess.TimeoutExpired(["docker"], 10),
        FileNotFoundError("docker"),
    ],
)
def test_container_ids_are_unknown_when_compose_cannot_list_them(
    outcome: subprocess.CompletedProcess[str] | Exception,
) -> None:
    with patch("codebase_rag.stack.manager.subprocess.run", side_effect=[outcome]):
        assert REAL_CONTAINER_IDS(["docker", "compose", "ps"], {}) is None


@pytest.mark.usefixtures("credentials")
def test_up_refused_before_starting_stops_nothing(tmp_path: Path) -> None:
    # It started nothing, so a stack already running is not its to stop.
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            return_value=_compose_config(UNRESOLVED_ENV),
        ),
        _left_running(mgr, memgraph_open=True) as stop,
        pytest.raises(StackError, match="does not come from"),
    ):
        mgr.up()

    stop.assert_not_called()


@pytest.mark.usefixtures("credentials")
def test_a_started_qdrant_that_rejects_the_key_is_refused_but_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another key protects it, so there is nothing open to stop.
    monkeypatch.setattr(settings, "QDRANT_ALLOW_INSECURE_API_KEY", True)
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch("codebase_rag.stack.manager.qdrant_accepts_key", return_value=False),
        _left_running(mgr, memgraph_open=False) as stop,
        pytest.raises(StackError, match="rejects the configured QDRANT_API_KEY"),
    ):
        mgr.wait_healthy(0)

    stop.assert_not_called()


def test_stop_keeps_the_containers(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.ensure_compose_file()
    with (
        patch(
            "codebase_rag.stack.manager.shutil.which", return_value="/usr/bin/docker"
        ),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as run,
    ):
        mgr.stop()
        mgr.stop(cs.SERVICE_MEMGRAPH)

    everything, one_service = run.call_args_list
    assert everything.args[0][-1] == "stop"
    assert one_service.args[0][-2:] == ["stop", cs.SERVICE_MEMGRAPH]


@pytest.mark.usefixtures("credentials")
def test_restart_checks_the_restarted_stack_enforces_the_credentials(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    with (
        patch.object(stack_cli, "StackManager", return_value=mgr),
        patch.object(mgr, "restart"),
        patch.object(mgr, "stop") as stop,
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous", return_value=True
        ),
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_anonymous", return_value=False
        ),
    ):
        result = CliRunner().invoke(stack_cli.cli, ["restart"])

    assert result.exit_code == 1
    assert "still accept" in result.output
    stop.assert_called_once_with(cs.SERVICE_MEMGRAPH)


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


@pytest.mark.parametrize(
    ("error", "rejected"),
    [
        ("Authentication failure", True),
        ("couldn't connect to host: Connection refused", False),
    ],
)
def test_memgraph_rejected_login_is_told_apart_from_a_refused_connection(
    error: str, rejected: bool
) -> None:
    # Memgraph 3.3's client raises OperationalError for both; only the
    # message differs.
    failure = health.mgclient.OperationalError(error)
    with patch.object(health.mgclient, "connect", side_effect=failure):
        assert (
            health.memgraph_rejects_credentials("localhost", 7687, ("cgr", "s3cret"))
            is rejected
        )


def test_memgraph_accepting_the_login_is_not_a_rejection() -> None:
    with patch.object(health.mgclient, "connect") as connect:
        assert not health.memgraph_rejects_credentials(
            "localhost", 7687, ("cgr", "s3cret")
        )

    connect.return_value.close.assert_called_once()


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


def test_qdrant_key_probe_is_a_write_that_changes_nothing() -> None:
    # Listing collections also works with a read-only key, which cannot index.
    with patch.object(
        health._DIRECT_OPENER, "open", return_value=_ok_response()
    ) as open_url:
        health.qdrant_accepts_key(6333, "qdrant-key")

    request = open_url.call_args.args[0]
    assert request.get_method() == "POST"
    assert request.full_url == "http://127.0.0.1:6333/collections/aliases"
    assert json.loads(request.data) == {"actions": []}
    assert request.get_header("Content-type") == "application/json"


@pytest.mark.parametrize("status", [401, 403])
def test_qdrant_key_probe_is_false_for_an_unknown_or_read_only_key(
    status: int,
) -> None:
    # Qdrant 1.19 answers the probe 401 for an unknown key and 403 for its
    # read-only key.
    refused = urllib.error.HTTPError(
        "http://127.0.0.1:6333/collections/aliases", status, "", Message(), None
    )
    with patch.object(health._DIRECT_OPENER, "open", side_effect=refused):
        assert not health.qdrant_accepts_key(6333, "qdrant-key")


def test_qdrant_anonymous_probe_is_false_when_the_key_is_required() -> None:
    unauthorized = urllib.error.HTTPError(
        "http://127.0.0.1:6333/collections", 401, "Unauthorized", Message(), None
    )
    with patch.object(health._DIRECT_OPENER, "open", side_effect=unauthorized):
        assert not health.qdrant_accepts_anonymous(6333)


@pytest.mark.parametrize(
    ("failure", "access"),
    [
        ("Authentication failure", cs.AnonymousAccess.REFUSED),
        (
            "couldn't connect to host: Connection refused",
            cs.AnonymousAccess.NO_ANSWER,
        ),
    ],
)
def test_memgraph_anonymous_access_tells_a_refusal_from_no_answer(
    failure: str, access: cs.AnonymousAccess
) -> None:
    # Memgraph 3.3 refuses an anonymous login at connect with this message.
    error = health.mgclient.OperationalError(failure)
    with patch.object(health.mgclient, "connect", side_effect=error):
        assert health.memgraph_anonymous_access("localhost", 7687) is access


def test_memgraph_anonymous_access_is_allowed_when_a_query_runs() -> None:
    with patch.object(health.mgclient, "connect") as connect:
        access = health.memgraph_anonymous_access("localhost", 7687)

    assert access is cs.AnonymousAccess.ALLOWED
    connect.assert_called_once_with(host="localhost", port=7687)
    connect.return_value.close.assert_called_once()


@pytest.mark.parametrize(
    ("status", "access"),
    [
        (401, cs.AnonymousAccess.REFUSED),
        (403, cs.AnonymousAccess.REFUSED),
        (503, cs.AnonymousAccess.NO_ANSWER),
    ],
)
def test_qdrant_anonymous_access_reads_the_status(
    status: int, access: cs.AnonymousAccess
) -> None:
    answer = urllib.error.HTTPError(
        "http://127.0.0.1:6333/collections", status, "", Message(), None
    )
    with patch.object(health._DIRECT_OPENER, "open", side_effect=answer):
        assert health.qdrant_anonymous_access(6333) is access


def test_qdrant_anonymous_access_is_allowed_or_unanswered() -> None:
    with patch.object(health._DIRECT_OPENER, "open", return_value=_ok_response()):
        assert health.qdrant_anonymous_access(6333) is cs.AnonymousAccess.ALLOWED
    refused = urllib.error.URLError(ConnectionRefusedError())
    with patch.object(health._DIRECT_OPENER, "open", side_effect=refused):
        assert health.qdrant_anonymous_access(6333) is cs.AnonymousAccess.NO_ANSWER


@pytest.fixture
def local_qdrant_port() -> Iterator[int]:
    """A loopback HTTP server that answers 200, standing in for Qdrant."""

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
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


@contextlib.contextmanager
def _loopback_server(
    handler: type[http.server.BaseHTTPRequestHandler],
) -> Iterator[int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_qdrant_key_probe_does_not_follow_a_redirect_elsewhere() -> None:
    # urllib's default redirect handler copies request headers onto the new
    # request, so following this 302 would hand the api-key to another host.
    received: list[str | None] = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.headers.get(cs.QDRANT_API_KEY_HEADER))
            self.send_response(200)
            self.end_headers()

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            return

    with _loopback_server(Elsewhere) as elsewhere_port:

        class Redirecting(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{elsewhere_port}/")
                self.end_headers()

            def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
                return

        with _loopback_server(Redirecting) as qdrant_port:
            assert not health.qdrant_accepts_key(qdrant_port, "qdrant-key")

    assert received == []


def _ensure_running_on_a_healthy_stack(
    mgr: StackManager,
    memgraph_open: bool,
    qdrant_open: bool,
    qdrant_key_accepted: bool = True,
) -> tuple[str | None, MagicMock, MagicMock, MagicMock]:
    """The error `ensure_running` refuses the running stack with, if any."""
    error: str | None = None
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
        ) as key_probe,
    ):
        try:
            mgr.ensure_running()
        except StackError as refused:
            error = str(refused)
    return error, memgraph_probe, qdrant_probe, key_probe


@pytest.mark.usefixtures("credentials")
def test_running_stack_that_accepts_anonymous_access_is_refused(
    tmp_path: Path,
) -> None:
    # A stack created before the credentials were set stays open, and its
    # health checks pass either way.
    error, _, _, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path), memgraph_open=True, qdrant_open=True
    )

    assert error is not None
    assert "still accept" in error
    assert cs.SERVICE_MEMGRAPH in error
    assert cs.SERVICE_QDRANT in error


@pytest.mark.usefixtures("credentials")
def test_running_stack_that_requires_the_credentials_is_used(
    tmp_path: Path,
) -> None:
    error, _, _, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path), memgraph_open=False, qdrant_open=False
    )

    assert error is None


@pytest.mark.usefixtures("no_credentials")
def test_running_stack_without_credentials_is_not_probed(tmp_path: Path) -> None:
    error, memgraph_probe, qdrant_probe, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path), memgraph_open=True, qdrant_open=True
    )

    memgraph_probe.assert_not_called()
    qdrant_probe.assert_not_called()
    assert error is None


@pytest.mark.usefixtures("credentials")
def test_running_qdrant_with_an_earlier_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anonymous requests are rejected, so only trying the configured key shows
    # that the app's requests would be rejected too.
    monkeypatch.setattr(settings, "QDRANT_ALLOW_INSECURE_API_KEY", True)

    error, _, _, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path),
        memgraph_open=False,
        qdrant_open=False,
        qdrant_key_accepted=False,
    )

    assert error is not None
    assert "rejects the configured QDRANT_API_KEY" in error


@pytest.mark.usefixtures("credentials")
def test_running_qdrant_is_not_sent_the_key_without_the_plain_http_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The probe is plain http, and the app itself refuses to send the key
    # over plain http without the opt-in.
    monkeypatch.setattr(settings, "QDRANT_ALLOW_INSECURE_API_KEY", False)

    error, _, _, key_probe = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path),
        memgraph_open=False,
        qdrant_open=False,
        qdrant_key_accepted=False,
    )

    key_probe.assert_not_called()
    assert error is None


@pytest.mark.usefixtures("credentials")
def test_up_names_a_running_memgraph_that_rejects_the_login(tmp_path: Path) -> None:
    # Memgraph keeps the password its user was created with, so after
    # MEMGRAPH_PASSWORD changes it refuses every probe while otherwise fine.
    # Starting it again would not help.
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=False),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch(
            "codebase_rag.stack.manager.memgraph_rejects_credentials",
            return_value=True,
        ),
        patch.object(mgr, "up") as up,
    ):
        with pytest.raises(StackError, match="rejects the configured"):
            mgr.ensure_running()

    up.assert_not_called()


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("rejected", "message"),
    [(True, "rejects the configured"), (False, "did not become healthy")],
)
def test_wait_healthy_says_whether_memgraph_rejected_the_login(
    tmp_path: Path, rejected: bool, message: str
) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=False),
        patch(
            "codebase_rag.stack.manager.memgraph_rejects_credentials",
            return_value=rejected,
        ),
        _left_running(mgr, memgraph_open=False),
    ):
        with pytest.raises(StackError, match=message):
            mgr.wait_healthy(timeout=0)


@pytest.mark.usefixtures("no_credentials")
def test_wait_healthy_without_credentials_does_not_try_a_login(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=False),
        patch("codebase_rag.stack.manager.memgraph_rejects_credentials") as probe,
        patch("codebase_rag.stack.manager.memgraph_anonymous_access") as memgraph_open,
        patch("codebase_rag.stack.manager.qdrant_anonymous_access") as qdrant_open,
    ):
        with pytest.raises(StackError, match="did not become healthy"):
            mgr.wait_healthy(timeout=0)

    probe.assert_not_called()
    # Nothing is protected, so nothing left open is looked for either.
    memgraph_open.assert_not_called()
    qdrant_open.assert_not_called()


class TestAppStartOnARunningStack:
    """The app's own start path returns early for a running stack instead of
    going through ensure_running, so it runs the authentication check too,
    and stops the app the way a failed start does."""

    @pytest.fixture
    def _disable_stack_autostart(self) -> None:
        # Exercise the real `_maybe_start_stack` instead of conftest's mock.
        return

    @staticmethod
    def _start_on_a_running_stack(
        mgr: StackManager, open_access: bool
    ) -> tuple[str, int | None, MagicMock]:
        """What the app printed, its exit code if it stopped, and the start."""
        from codebase_rag.cli import _maybe_start_stack
        from codebase_rag.cli_runtime import app_context

        exit_code: int | None = None
        with (
            patch("codebase_rag.cli.StackManager", return_value=mgr),
            patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
            patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
            patch(
                "codebase_rag.stack.manager.memgraph_accepts_anonymous",
                return_value=open_access,
            ),
            patch(
                "codebase_rag.stack.manager.qdrant_accepts_anonymous",
                return_value=open_access,
            ),
            patch.object(app_context.console, "print") as printed,
            patch.object(mgr, "ensure_running") as ensure_running,
        ):
            try:
                _maybe_start_stack()
            except typer.Exit as exited:
                exit_code = exited.exit_code
        shown = " ".join(str(call.args[0]) for call in printed.call_args_list)
        return shown, exit_code, ensure_running

    @pytest.mark.usefixtures("credentials")
    def test_stops_on_a_running_stack_that_accepts_anonymous_access(
        self, tmp_path: Path
    ) -> None:
        shown, exit_code, _ = self._start_on_a_running_stack(
            _manager(tmp_path), open_access=True
        )

        assert exit_code == 1
        assert "still accept" in shown
        assert cs.SERVICE_MEMGRAPH in shown
        assert cs.SERVICE_QDRANT in shown

    @pytest.mark.usefixtures("credentials")
    def test_carries_on_with_a_running_stack_that_requires_the_credentials(
        self, tmp_path: Path
    ) -> None:
        shown, exit_code, ensure_running = self._start_on_a_running_stack(
            _manager(tmp_path), open_access=False
        )

        assert exit_code is None
        assert shown == ""
        ensure_running.assert_not_called()


@pytest.mark.parametrize(
    ("url", "bind", "port", "named"),
    [
        ("http://localhost:6333", "127.0.0.1", 6333, True),
        ("http://127.0.0.1:6333", "127.0.0.1", 6333, True),
        # qdrant-client connects to 6333 when the URL names no port.
        ("http://localhost", "127.0.0.1", 6333, True),
        ("http://localhost:16333", "127.0.0.1", 16333, True),
        # Another Qdrant on this machine: another port, or another address
        # than the one the bundled Qdrant is published on.
        ("http://localhost:7333", "127.0.0.1", 6333, False),
        ("http://127.0.0.2:6333", "127.0.0.1", 6333, False),
        # A wildcard bind, or none, publishes on every address here.
        ("http://127.0.0.1:16333", "0.0.0.0", 16333, True),
        ("http://localhost:16333", "", 16333, True),
        # A documentation address (RFC 5737) that no interface here has.
        ("https://203.0.113.7:6333", "0.0.0.0", 6333, False),
        ("http://localhost:notaport", "127.0.0.1", 6333, False),
        ("not a url", "127.0.0.1", 6333, False),
        (None, "127.0.0.1", 6333, False),
    ],
)
def test_qdrant_url_naming_the_bundled_qdrant_is_recognised(
    url: str | None, bind: str, port: int, named: bool
) -> None:
    assert manager_module._names_published_qdrant(url, bind, port) is named


@pytest.mark.parametrize(
    ("url", "named"),
    [("http://192.0.2.2:16333", True), ("http://192.0.2.3:16333", False)],
)
def test_a_wildcard_bind_covers_every_address_of_this_machine(
    monkeypatch: pytest.MonkeyPatch, url: str, named: bool
) -> None:
    # Which addresses a machine has differs between hosts: macOS gives its
    # loopback interface 127.0.0.1 alone, so 127.0.0.2 is local on Linux only.
    monkeypatch.setattr(
        manager_module, "_is_local_address", lambda host: host == "192.0.2.2"
    )

    assert manager_module._names_published_qdrant(url, "0.0.0.0", 16333) is named


@pytest.mark.usefixtures("credentials")
def test_a_key_for_another_qdrant_is_not_copied_into_the_local_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Qdrant Cloud key would otherwise sit in the local container's
    # environment and be sent to whatever answers on the local port.
    monkeypatch.setattr(settings, "QDRANT_URL", "https://203.0.113.7:6333")
    monkeypatch.setattr(settings, "QDRANT_ALLOW_INSECURE_API_KEY", True)
    mgr = _manager(tmp_path)

    assert cs.ENV_QDRANT_API_KEY not in mgr._compose_env()
    error, _, qdrant_probe, key_probe = _ensure_running_on_a_healthy_stack(
        mgr, memgraph_open=False, qdrant_open=True, qdrant_key_accepted=False
    )
    qdrant_probe.assert_not_called()
    key_probe.assert_not_called()
    assert error is None


def test_local_address_is_one_of_this_machines_addresses() -> None:
    assert manager_module._is_local_address("127.0.0.1")
    # A documentation address (RFC 5737) that no interface here has.
    assert not manager_module._is_local_address("203.0.113.1")


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("url", "forwarded"),
    [
        # Compose publishes on 0.0.0.0:16333 through settings the manager
        # cannot see, such as a file named in COMPOSE_ENV_FILES; the key
        # follows Compose's answer, so a URL naming that endpoint gets it.
        ("http://192.168.1.5:16333", True),
        ("http://localhost:16333", True),
        # Another Qdrant on this machine, on the default port.
        ("http://localhost:6333", False),
        ("http://192.168.1.6:16333", False),
    ],
)
def test_up_gives_the_key_to_the_qdrant_the_url_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, forwarded: bool
) -> None:
    monkeypatch.setattr(settings, "QDRANT_URL", url)
    monkeypatch.setattr(
        manager_module,
        "_is_local_address",
        lambda host: host in ("192.168.1.5", "localhost"),
    )
    published = {cs.SERVICE_QDRANT: _qdrant_ports("0.0.0.0", "16333")}
    resolved_env = MATCHING_ENV if forwarded else _without_qdrant_key(MATCHING_ENV)
    mgr = _manager(tmp_path)
    up_env = _up_environment(mgr, _compose_config(resolved_env, published))

    assert (up_env.get(cs.ENV_QDRANT_API_KEY) == "qdrant-key") is forwarded


@pytest.mark.usefixtures("credentials")
def test_a_status_check_does_not_resolve_the_qdrant_url(tmp_path: Path) -> None:
    # Resolving a host name can wait on DNS; only the key decision needs it.
    mgr = _manager(tmp_path)
    with (
        patch.object(manager_module, "_is_local_address") as resolve,
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
    ):
        mgr.status()

    resolve.assert_not_called()


@pytest.mark.parametrize(
    ("bind", "probe_host"),
    [
        (None, "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("0.0.0.0", "127.0.0.1"),
        ("::", "127.0.0.1"),
        ("192.168.1.5", "192.168.1.5"),
    ],
)
def test_stack_reaches_qdrant_where_it_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind: str | None, probe_host: str
) -> None:
    if bind is None:
        monkeypatch.delenv(cs.COMPOSE_BIND_HOST_VAR, raising=False)
    else:
        monkeypatch.setenv(cs.COMPOSE_BIND_HOST_VAR, bind)

    mgr = _manager(tmp_path)
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch(
            "codebase_rag.stack.manager.wait_for_qdrant", return_value=True
        ) as ready_probe,
    ):
        status = mgr.status()

    assert ready_probe.call_args.kwargs["host"] == probe_host
    assert status.qdrant_endpoint == f"{probe_host}:6333"


@pytest.mark.usefixtures("credentials")
def test_running_stack_checks_qdrant_on_the_bind_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Published on one LAN address, the bundled Qdrant is not on loopback.
    monkeypatch.setenv(cs.COMPOSE_BIND_HOST_VAR, "192.168.1.5")
    monkeypatch.setattr(settings, "QDRANT_URL", "http://192.168.1.5:6333")
    monkeypatch.setattr(
        manager_module, "_is_local_address", lambda host: host == "192.168.1.5"
    )

    error, _, qdrant_probe, _ = _ensure_running_on_a_healthy_stack(
        _manager(tmp_path), memgraph_open=False, qdrant_open=True
    )

    qdrant_probe.assert_called_once_with(6333, host="192.168.1.5")
    assert error is not None
    assert "still accept" in error


def test_qdrant_probe_brackets_an_ipv6_address() -> None:
    with patch.object(
        health._DIRECT_OPENER, "open", return_value=_ok_response()
    ) as open_url:
        assert health.qdrant_accepts_anonymous(6333, host="::1")

    assert open_url.call_args.args[0].full_url == "http://[::1]:6333/collections"


def test_qdrant_readiness_probe_uses_the_given_host() -> None:
    with patch.object(health, "_http_reachable", return_value=True) as reachable:
        assert health.wait_for_qdrant(6333, timeout=1, host="192.168.1.5")

    reachable.assert_called_once_with("http://192.168.1.5:6333/readyz")


@pytest.mark.parametrize(
    ("environment", "dotenv", "bind"),
    [
        (None, None, "127.0.0.1"),
        ("192.168.1.5", None, "192.168.1.5"),
        # Compose reads the .env beside the compose file for an unset variable.
        (None, "CGR_STACK_BIND_HOST=0.0.0.0\n", "0.0.0.0"),
        # Its environment wins over that file.
        ("192.168.1.5", "CGR_STACK_BIND_HOST=0.0.0.0\n", "192.168.1.5"),
        # `${CGR_STACK_BIND_HOST:-127.0.0.1}` treats an empty value as unset.
        ("", "CGR_STACK_BIND_HOST=0.0.0.0\n", "127.0.0.1"),
        (None, "CGR_STACK_BIND_HOST=\n", "127.0.0.1"),
    ],
)
def test_bind_host_is_resolved_as_compose_resolves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str | None,
    dotenv: str | None,
    bind: str,
) -> None:
    if environment is None:
        monkeypatch.delenv(cs.COMPOSE_BIND_HOST_VAR, raising=False)
    else:
        monkeypatch.setenv(cs.COMPOSE_BIND_HOST_VAR, environment)
    if dotenv is not None:
        (tmp_path / cs.COMPOSE_DOTENV_FILENAME).write_text(dotenv)

    assert manager_module._bind_host(tmp_path) == bind


def test_probes_follow_a_bind_from_the_compose_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cs.COMPOSE_BIND_HOST_VAR, raising=False)
    home = tmp_path / "cgr-home"
    home.mkdir()
    (home / cs.COMPOSE_DOTENV_FILENAME).write_text("CGR_STACK_BIND_HOST=192.168.1.5\n")

    assert _manager(tmp_path).qdrant_host == "192.168.1.5"


def _qdrant_ports(host_ip: str | None, published: str) -> list[_PortEntry]:
    """Qdrant's `ports` as `docker compose config` renders them."""
    http: _PortEntry = {"mode": "ingress", "target": 6333, "published": published}
    grpc: _PortEntry = {"mode": "ingress", "target": 6334, "published": "6334"}
    if host_ip is not None:
        http["host_ip"] = grpc["host_ip"] = host_ip
    return [grpc, http]


def _without_qdrant_key(
    environments: dict[str, dict[str, str | None]],
) -> dict[str, dict[str, str | None]]:
    return {**environments, cs.SERVICE_QDRANT: {cs.ENV_QDRANT_API_KEY: None}}


def _up_environment(
    mgr: StackManager, config: subprocess.CompletedProcess[str]
) -> dict[str, str]:
    """The environment `up` starts the containers in, Compose answering `config`."""
    started: list[dict[str, str]] = []

    def run(
        cmd: list[str], *, env: dict[str, str], **_: bool | str | float
    ) -> subprocess.CompletedProcess[str]:
        if cmd[-2:] == ["up", "-d"]:
            started.append(env)
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        return config

    with (
        patch.object(mgr, "check_docker"),
        patch("codebase_rag.stack.manager.subprocess.run", side_effect=run),
    ):
        mgr.up()
    (env,) = started
    return env


@pytest.mark.usefixtures("credentials")
def test_running_stack_is_checked_where_compose_publishes_qdrant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # QDRANT_HTTP_PORT, from wherever Compose read it, moved the bundled
    # Qdrant; another service answering on 6333 must not be checked instead.
    monkeypatch.setattr(settings, "QDRANT_URL", "http://localhost:16333")
    mgr = _manager(tmp_path)
    mgr.ensure_compose_file()
    config = _compose_config(
        MATCHING_ENV, {cs.SERVICE_QDRANT: _qdrant_ports("127.0.0.1", "16333")}
    )
    with (
        patch(
            "codebase_rag.stack.manager.shutil.which", return_value="/usr/bin/docker"
        ),
        patch("codebase_rag.stack.manager.subprocess.run", return_value=config),
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch(
            "codebase_rag.stack.manager.wait_for_qdrant", return_value=True
        ) as ready_probe,
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous", return_value=False
        ),
        patch(
            "codebase_rag.stack.manager.qdrant_accepts_anonymous", return_value=True
        ) as qdrant_probe,
        pytest.raises(StackError, match="still accept"),
    ):
        mgr.ensure_running()

    assert ready_probe.call_args.args[0] == 16333
    qdrant_probe.assert_called_once_with(16333, host="127.0.0.1")


@pytest.mark.usefixtures("credentials")
def test_running_stack_is_not_located_without_a_qdrant_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a key only readiness is probed, as before: no Docker call.
    monkeypatch.setattr(settings, "QDRANT_API_KEY", None)
    mgr = _manager(tmp_path)
    mgr.ensure_compose_file()
    with (
        patch(
            "codebase_rag.stack.manager.shutil.which", return_value="/usr/bin/docker"
        ),
        patch("codebase_rag.stack.manager.subprocess.run") as run,
    ):
        mgr.locate_published_qdrant()

    run.assert_not_called()


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("host_ip", "published", "endpoint"),
    [
        ("192.168.1.5", "16333", ("192.168.1.5", 16333)),
        ("127.0.0.1", "6333", ("127.0.0.1", 6333)),
        ("0.0.0.0", "6333", ("127.0.0.1", 6333)),
        (None, "16333", ("127.0.0.1", 16333)),
    ],
)
def test_up_probes_qdrant_where_compose_publishes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_ip: str | None,
    published: str,
    endpoint: tuple[str, int],
) -> None:
    # A file named in COMPOSE_ENV_FILES, among others, can move the bind or
    # the port without the manager seeing it; Compose's answer covers them all.
    monkeypatch.setattr(settings, "QDRANT_API_KEY", None)
    config = _compose_config(
        _without_qdrant_key(MATCHING_ENV),
        {cs.SERVICE_QDRANT: _qdrant_ports(host_ip, published)},
    )
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            side_effect=[config, subprocess.CompletedProcess(args=[], returncode=0)],
        ),
    ):
        mgr.up()
    with (
        patch("codebase_rag.stack.manager.wait_for_memgraph", return_value=True),
        patch(
            "codebase_rag.stack.manager.wait_for_qdrant", return_value=True
        ) as qdrant_probe,
        patch(
            "codebase_rag.stack.manager.memgraph_accepts_anonymous", return_value=False
        ),
    ):
        mgr.wait_healthy()

    probed = (mgr.qdrant_host, mgr.qdrant_port)
    assert probed == endpoint
    assert qdrant_probe.call_args.args[0] == endpoint[1]
    assert qdrant_probe.call_args.kwargs["host"] == endpoint[0]


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    "ports",
    [None, [], [{"mode": "ingress", "target": 6334, "published": "6334"}]],
)
def test_up_keeps_the_probe_endpoint_without_a_qdrant_port_entry(
    tmp_path: Path, ports: list[_PortEntry] | None
) -> None:
    config = _compose_config(
        MATCHING_ENV, None if ports is None else {cs.SERVICE_QDRANT: ports}
    )
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch(
            "codebase_rag.stack.manager.subprocess.run",
            side_effect=[config, subprocess.CompletedProcess(args=[], returncode=0)],
        ),
    ):
        mgr.up()

    probed = (mgr.qdrant_host, mgr.qdrant_port)
    assert probed == ("127.0.0.1", 6333)


@pytest.mark.usefixtures("credentials")
@pytest.mark.parametrize(
    ("entry", "shown"),
    [
        # As Compose renders `"6333"`, `"127.0.0.1::6333"`, a range and
        # QDRANT_HTTP_PORT=0: Docker picks each of these at start.
        ({"mode": "ingress", "target": 6333}, "none"),
        ({"mode": "ingress", "host_ip": "127.0.0.1", "target": 6333}, "none"),
        (
            {
                "mode": "ingress",
                "host_ip": "127.0.0.1",
                "target": 6333,
                "published": "16333-16340",
            },
            "'16333-16340'",
        ),
        (
            {
                "mode": "ingress",
                "host_ip": "127.0.0.1",
                "target": 6333,
                "published": "0",
            },
            "'0'",
        ),
    ],
)
def test_up_refuses_a_qdrant_port_docker_picks_at_start(
    tmp_path: Path, entry: _PortEntry, shown: str
) -> None:
    config = _compose_config(MATCHING_ENV, {cs.SERVICE_QDRANT: [entry]})
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "check_docker"),
        patch("codebase_rag.stack.manager.subprocess.run", return_value=config) as run,
        pytest.raises(StackError, match="no fixed host port") as raised,
    ):
        mgr.up()

    assert f"resolves it to {shown})" in str(raised.value)
    assert run.call_count == 1


def test_qdrant_readiness_probe_bypasses_an_http_proxy(
    local_qdrant_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The proxy is a closed port, so the probe only succeeds directly.
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    assert health.wait_for_qdrant(local_qdrant_port, timeout=2, interval=0.1)
