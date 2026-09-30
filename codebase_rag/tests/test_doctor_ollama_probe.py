"""Issue #2423: `cgr doctor` says an Ollama model is ready only when Ollama
answers and has the model.

The model check only asked whether the role's credentials would pass the
start-up gate. Ollama needs none, so with nothing listening on
localhost:11434 doctor still printed "Orchestrator model ready
(ollama:llama3.2)", the one thing it exists to catch before a first
`cgr start` fails. Key-based providers are not probed over the network, so
their passing state now says what was checked: the credentials.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.schemas import HealthCheckResult
from codebase_rag.tools.health_checker import HealthChecker


@pytest.fixture(autouse=True)
def default_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    for role in ("ORCHESTRATOR", "CYPHER"):
        monkeypatch.setattr(settings, f"{role}_PROVIDER", "")
        monkeypatch.setattr(settings, f"{role}_MODEL", "")
        monkeypatch.setattr(settings, f"{role}_API_KEY", None)
        monkeypatch.setattr(settings, f"{role}_ENDPOINT", None)
        monkeypatch.setattr(settings, f"{role}_PROVIDER_TYPE", None)
    monkeypatch.setattr(settings, "_active_orchestrator", None)
    monkeypatch.setattr(settings, "_active_cypher", None)


def _serving(body: bytes, requests: list[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: str | int) -> None:
            pass

    return Handler


@contextmanager
def _ollama(body: bytes) -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []
    server = HTTPServer(("127.0.0.1", 0), _serving(body, requests))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()


def _tags(*names: str) -> bytes:
    return json.dumps({"models": [{"name": n, "model": n} for n in names]}).encode()


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _orchestrator() -> HealthCheckResult:
    return HealthChecker().check_model_role(cs.ModelRole.ORCHESTRATOR)


@pytest.fixture
def nothing_listening(monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"http://127.0.0.1:{_unused_port()}"
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", url)
    return url


def test_no_ollama_server_is_not_ready(nothing_listening: str) -> None:
    result = _orchestrator()

    assert not result.passed
    assert "ready" not in result.name.replace("not ", "")
    assert nothing_listening in (result.error or "")
    assert "ORCHESTRATOR_PROVIDER" in (result.error or "")


def test_a_model_not_pulled_says_how_to_pull_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _ollama(_tags("qwen2.5:7b")) as (url, _):
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", url)
        result = _orchestrator()

    assert not result.passed
    assert f"ollama pull {cs.DEFAULT_MODEL}" in (result.error or "")


@pytest.mark.parametrize(
    ("configured", "pulled"),
    [
        (cs.DEFAULT_MODEL, f"{cs.DEFAULT_MODEL}:latest"),
        (cs.DEFAULT_MODEL, cs.DEFAULT_MODEL),
        ("llama3.2:3b", "llama3.2:3b"),
    ],
)
def test_a_pulled_model_on_a_running_server_is_ready(
    monkeypatch: pytest.MonkeyPatch, configured: str, pulled: str
) -> None:
    monkeypatch.setattr(settings, "ORCHESTRATOR_PROVIDER", cs.Provider.OLLAMA)
    monkeypatch.setattr(settings, "ORCHESTRATOR_MODEL", configured)
    with _ollama(_tags(pulled)) as (url, requests):
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", url)
        result = _orchestrator()

    assert result.passed, result
    assert "ready" in result.name
    assert requests == [cs.OLLAMA_HEALTH_PATH]


def test_another_tag_of_the_model_is_not_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: `llama3.2:3b` is configured, only `llama3.2:latest` is pulled.
    monkeypatch.setattr(settings, "ORCHESTRATOR_PROVIDER", cs.Provider.OLLAMA)
    monkeypatch.setattr(settings, "ORCHESTRATOR_MODEL", "llama3.2:3b")
    with _ollama(_tags("llama3.2:latest")) as (url, _):
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", url)
        result = _orchestrator()

    assert not result.passed


def test_something_else_on_the_port_is_not_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _ollama(b"<html>not ollama</html>") as (url, _):
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", url)
        result = _orchestrator()

    assert not result.passed


def test_a_key_based_provider_reports_credentials_without_a_probe(
    monkeypatch: pytest.MonkeyPatch, nothing_listening: str
) -> None:
    # Negative for the probe: nothing is fetched for a key-based provider,
    # and its passing state no longer claims "ready".
    monkeypatch.setattr(settings, "ORCHESTRATOR_PROVIDER", cs.Provider.OPENAI)
    monkeypatch.setattr(settings, "ORCHESTRATOR_MODEL", "gpt-4o")
    monkeypatch.setattr(settings, "ORCHESTRATOR_API_KEY", "sk-test")

    result = _orchestrator()

    assert result.passed, result
    assert "credentials" in result.name
    assert "ready" not in result.name


def test_a_key_based_provider_without_a_key_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative.
    monkeypatch.setattr(settings, "ORCHESTRATOR_PROVIDER", cs.Provider.OPENAI)
    monkeypatch.setattr(settings, "ORCHESTRATOR_MODEL", "gpt-4o")
    monkeypatch.delenv(cs.ENV_OPENAI_API_KEY, raising=False)

    assert not _orchestrator().passed


@pytest.mark.parametrize("prefix", ["/ollama", "/proxy/ollama/"])
def test_an_ollama_behind_a_path_prefix_is_probed_under_it(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    # Review of PR 2502: `urljoin` with the absolute `/api/tags` dropped the
    # endpoint's own path, so an Ollama served at `https://host/ollama` was
    # probed at `https://host/api/tags` and reported as not running.
    with _ollama(_tags(cs.DEFAULT_MODEL)) as (url, requests):
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", f"{url}{prefix}")
        _orchestrator()

    assert requests == [f"{prefix.rstrip('/')}{cs.OLLAMA_HEALTH_PATH}"]


@pytest.mark.parametrize("prefix", ["/ollama", "/ollama/v1"])
def test_the_provider_probes_the_same_url_as_doctor(prefix: str) -> None:
    # The provider's own start-up check must agree with doctor, or doctor
    # says ready and `cgr start` then refuses the same endpoint.
    from codebase_rag.providers.base import OllamaProvider

    with _ollama(_tags(cs.DEFAULT_MODEL)) as (url, requests):
        OllamaProvider(endpoint=f"{url}{prefix}").validate_config()

    assert requests == [f"/ollama{cs.OLLAMA_HEALTH_PATH}"]


@pytest.mark.parametrize("configured", ["", "/"])
def test_an_endpoint_without_a_path_is_probed_at_the_root(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    # Negative: no prefix, and no doubled slash from a trailing one.
    with _ollama(_tags(cs.DEFAULT_MODEL)) as (url, requests):
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", f"{url}{configured}")
        result = _orchestrator()

    assert result.passed, result
    assert requests == [cs.OLLAMA_HEALTH_PATH]
