"""Issue #2355: with the stack up, embeddings go to the stack's Qdrant.

`cgr daemon up` starts a Qdrant server, but with QDRANT_URL unset the vector
store opened an embedded Qdrant at the cwd-relative QDRANT_DB_PATH, so the
vectors landed in a hidden folder inside the indexed repository and the
stack's Qdrant stayed empty.

The stack's Qdrant is adopted only where Compose's resolved configuration
publishes it, only while Compose reports that project's own Qdrant container
running and the endpoint answers as Qdrant, and only when QDRANT_DB_PATH was
not supplied at all: a guessed port, a stopped stack or a supplied path that
happens to equal the default must never send embeddings anywhere else.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import vector_store as vs
from codebase_rag.config import AppConfig, settings
from codebase_rag.stack import constants as stack_cs

_DB_PATH_SETTING = "QDRANT_DB_PATH"
# What a real Qdrant 1.19 answers on `GET /`.
_QDRANT_ROOT = {
    "title": "qdrant - vector search engine",
    "version": "1.19.1",
    "commit": "6ab21cac18ebb6f4ae29102c7f8f5cc11affd5de",
}
_QDRANT_COLLECTIONS = {"result": {"collections": []}, "status": "ok", "time": 0.0}
# Any other local service: it answers every request 200, and even names a
# version, but it is not Qdrant.
_OTHER_SERVICE = {"name": "some-dev-server", "version": "2.1.0"}
_RUNNING_CONTAINER_ID = "3f9c2a7d1b0e\n"


class _Handler(BaseHTTPRequestHandler):
    refuse_anonymous = False
    is_qdrant = True
    paths: list[str]

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
        self.paths.append(self.path)
        body: dict[str, str | float | dict[str, list[str]]] = {}
        if self.path.startswith(stack_cs.QDRANT_READY_PATH):
            status = 200
        elif not self.is_qdrant:
            status, body = 200, dict(_OTHER_SERVICE)
        elif self.refuse_anonymous:
            status = 401
        elif self.path == "/":
            status, body = 200, dict(_QDRANT_ROOT)
        else:
            status, body = 200, dict(_QDRANT_COLLECTIONS)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args: str | int) -> None:
        return None


@dataclass
class _Server:
    port: int
    paths: list[str] = field(default_factory=list)


def _serve(
    refuse_anonymous: bool = False, is_qdrant: bool = True
) -> Generator[_Server, None, None]:
    paths: list[str] = []
    handler = type(
        "Handler",
        (_Handler,),
        {"refuse_anonymous": refuse_anonymous, "is_qdrant": is_qdrant, "paths": paths},
    )
    server = ThreadingHTTPServer((stack_cs.LOOPBACK_HOST, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Server(port=server.server_address[1], paths=paths)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def open_qdrant() -> Generator[_Server, None, None]:
    yield from _serve()


@pytest.fixture
def decoy_qdrant() -> Generator[_Server, None, None]:
    yield from _serve()


@pytest.fixture
def keyed_qdrant() -> Generator[_Server, None, None]:
    yield from _serve(refuse_anonymous=True)


@pytest.fixture
def other_service() -> Generator[_Server, None, None]:
    yield from _serve(is_qdrant=False)


class _Compose:
    """Docker Compose as the stack manager asks it.

    `config` renders the project with Qdrant published where `published`
    says, the way Compose resolves it from every source (the environment,
    the .env beside the compose file, COMPOSE_ENV_FILES, edits to the file);
    `ps` lists the project's running Qdrant container, if `running`.
    """

    def __init__(self) -> None:
        self.docker_installed = True
        self.published: tuple[str, str] | None = (
            stack_cs.LOOPBACK_HOST,
            str(stack_cs.QDRANT_CLIENT_DEFAULT_PORT),
        )
        self.running = True
        self.failing: set[str] = set()
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def publish(self, port: int | str, host_ip: str = stack_cs.LOOPBACK_HOST) -> None:
        self.published = (host_ip, str(port))

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if self.docker_installed else None

    def run(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        **_: bool | str | float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        self.envs.append(env or {})
        subcommand = next(c for c in ("config", "ps") if c in cmd)
        if subcommand in self.failing:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        if subcommand == "ps":
            stdout = _RUNNING_CONTAINER_ID if self.running else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        ports = []
        if self.published is not None:
            host_ip, published = self.published
            ports.append(
                {
                    "mode": "ingress",
                    "host_ip": host_ip,
                    "target": stack_cs.QDRANT_CONTAINER_HTTP_PORT,
                    "published": published,
                    "protocol": "tcp",
                }
            )
        config = {"services": {stack_cs.SERVICE_QDRANT: {"ports": ports}}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(config), stderr="")


@pytest.fixture(autouse=True)
def compose(monkeypatch: pytest.MonkeyPatch) -> Generator[_Compose, None, None]:
    # No test here may reach the real Docker, or read the developer's own
    # Compose variables.
    for name in (
        stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR,
        stack_cs.COMPOSE_BIND_HOST_VAR,
        "COMPOSE_ENV_FILES",
    ):
        monkeypatch.delenv(name, raising=False)
    fake = _Compose()
    with (
        patch("codebase_rag.stack.manager.shutil.which", side_effect=fake.which),
        patch("codebase_rag.stack.manager.subprocess.run", side_effect=fake.run),
    ):
        yield fake


def _leave_db_path_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite-wide isolation assigns QDRANT_DB_PATH, which marks it as
    # supplied; these tests need it as a fresh process sees it when no
    # environment variable or .env line names it.
    monkeypatch.setattr(settings, _DB_PATH_SETTING, cs.QDRANT_DEFAULT_DB_PATH)
    monkeypatch.setattr(
        settings,
        "__pydantic_fields_set__",
        settings.model_fields_set - {_DB_PATH_SETTING},
    )


@pytest.fixture
def stack_home(_isolate_cgr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # `cgr daemon up` writes the compose file here; its presence is what
    # says the bundled stack was set up on this machine.
    (_isolate_cgr_home / stack_cs.COMPOSE_FILENAME).write_text("services: {}\n")
    _leave_db_path_unset(monkeypatch)
    return _isolate_cgr_home


def _stack_on(port: int, compose: _Compose, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stack publishes Qdrant on `port` because QDRANT_HTTP_PORT says so,
    which Compose then resolves into its configuration."""
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(port))
    compose.publish(port)


@pytest.fixture(autouse=True)
def _fresh_client() -> Generator[None, None, None]:
    with patch.object(
        vs.settings, "VECTOR_STORE_BACKEND", cs.VectorStoreBackend.QDRANT
    ):
        vs.close_vector_store_client()
        yield
        vs.close_vector_store_client()


def _client_kwargs() -> dict[str, object]:
    with patch("codebase_rag.vector_store.QdrantClient") as client_cls:
        client_cls.return_value = MagicMock(**{"collection_exists.return_value": True})
        vs.get_qdrant_client()
    client_cls.assert_called_once()
    return dict(client_cls.call_args.kwargs)


def _client_kwargs_logged(level: str) -> tuple[dict[str, object], list[str]]:
    messages: list[str] = []
    sink = logger.add(messages.append, level=level, format="{message}")
    try:
        return _client_kwargs(), messages
    finally:
        logger.remove(sink)


def _embedded() -> dict[str, object]:
    return {"path": cs.QDRANT_DEFAULT_DB_PATH}


def _bundled(port: int) -> dict[str, object]:
    return {"url": f"http://{stack_cs.LOOPBACK_HOST}:{port}", "api_key": None}


def test_the_running_stack_qdrant_receives_the_embeddings(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stack_on(open_qdrant.port, compose, monkeypatch)

    assert _client_kwargs() == _bundled(open_qdrant.port)


def test_the_port_is_read_from_the_env_file_beside_the_compose_file(
    stack_home: Path, open_qdrant: _Server, compose: _Compose
) -> None:
    # Compose reads the .env beside the compose file itself, so the port it
    # names reaches the resolved configuration.
    (stack_home / stack_cs.COMPOSE_DOTENV_FILENAME).write_text(
        f"{stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR}={open_qdrant.port}\n"
    )
    compose.publish(open_qdrant.port)

    assert _client_kwargs() == _bundled(open_qdrant.port)


def test_qdrant_is_probed_where_the_compose_config_publishes_it(
    stack_home: Path, open_qdrant: _Server, compose: _Compose
) -> None:
    # A user-edited compose file publishes Qdrant on a port of its own, with
    # no QDRANT_HTTP_PORT anywhere: only Compose's resolved configuration
    # knows it, and a guess of 6333 would miss the stack or find another
    # service there.
    (stack_home / stack_cs.COMPOSE_FILENAME).write_text(
        "services:\n  qdrant:\n    image: qdrant/qdrant\n    ports:\n"
        f'      - "127.0.0.1:{open_qdrant.port}:6333"\n'
    )
    compose.publish(open_qdrant.port)

    assert _client_kwargs() == _bundled(open_qdrant.port)
    assert open_qdrant.paths


def test_compose_env_files_decide_the_port_over_the_env_file_beside_it(
    stack_home: Path,
    open_qdrant: _Server,
    decoy_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # COMPOSE_ENV_FILES replaces the .env beside the compose file, so the
    # port that .env names is not where the stack's Qdrant is, and whatever
    # answers there must not get the embeddings.
    (stack_home / stack_cs.COMPOSE_DOTENV_FILENAME).write_text(
        f"{stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR}={decoy_qdrant.port}\n"
    )
    env_files = tmp_path / "stack.env"
    env_files.write_text(
        f"{stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR}={open_qdrant.port}\n"
    )
    monkeypatch.setenv("COMPOSE_ENV_FILES", str(env_files))
    compose.publish(open_qdrant.port)

    assert _client_kwargs() == _bundled(open_qdrant.port)
    assert decoy_qdrant.paths == []
    assert compose.envs[0]["COMPOSE_ENV_FILES"] == str(env_files)


def test_a_stopped_stack_does_not_hand_embeddings_to_another_service(
    stack_home: Path,
    other_service: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stack is set up but stopped, and an unrelated local service now
    # answers 200 on the port Qdrant is published on. Nothing is sent to it.
    _stack_on(other_service.port, compose, monkeypatch)
    compose.running = False

    kwargs, messages = _client_kwargs_logged("INFO")

    assert kwargs == _embedded()
    assert other_service.paths == []
    assert any("not running" in m for m in messages)


def test_a_stopped_stack_does_not_adopt_a_qdrant_it_does_not_own(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even a Qdrant on that port is someone else's while the stack's own
    # container is not running.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    compose.running = False

    assert _client_kwargs() == _embedded()
    assert open_qdrant.paths == []


def test_a_service_that_does_not_answer_as_qdrant_is_not_adopted(
    stack_home: Path,
    other_service: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Compose reports the stack's container running, but what answers where
    # it publishes Qdrant does not identify as Qdrant.
    _stack_on(other_service.port, compose, monkeypatch)

    kwargs, warnings = _client_kwargs_logged("WARNING")

    assert kwargs == _embedded()
    assert any(str(other_service.port) in w for w in warnings)


@pytest.mark.parametrize("failure", ["no docker", "config", "ps"])
def test_a_stack_compose_cannot_vouch_for_is_not_adopted(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    # Without Docker, or when Compose cannot render the project or list its
    # containers, the endpoint's owner is unknown: keep the embedded store
    # and say why.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    if failure == "no docker":
        compose.docker_installed = False
    else:
        compose.failing.add(failure)

    kwargs, messages = _client_kwargs_logged("INFO")

    assert kwargs == _embedded()
    assert open_qdrant.paths == []
    assert any("Docker Compose" in m for m in messages)


@pytest.mark.parametrize("published", [None, "", "0"])
def test_a_qdrant_compose_publishes_on_no_fixed_port_is_not_adopted(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    published: str | None,
) -> None:
    # No published port, or one Docker picks at start: there is no endpoint
    # to trust, and guessing one is what the resolved configuration avoids.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    compose.published = (
        None if published is None else (stack_cs.LOOPBACK_HOST, published)
    )

    assert _client_kwargs() == _embedded()
    assert open_qdrant.paths == []


def test_a_running_stack_on_the_default_port_is_still_adopted(
    stack_home: Path, compose: _Compose
) -> None:
    # Negative: the stack as `cgr daemon up` renders it, Qdrant published on
    # 127.0.0.1:6333 and running, is adopted as before. The probes are
    # stubbed because this machine may run its own Qdrant on 6333.
    default = stack_cs.QDRANT_CLIENT_DEFAULT_PORT
    compose.publish(default)
    with (
        patch(
            "codebase_rag.stack.manager.qdrant_anonymous_access",
            return_value=stack_cs.AnonymousAccess.ALLOWED,
        ) as access,
        patch(
            "codebase_rag.stack.manager.qdrant_identifies", return_value=True
        ) as identity,
    ):
        kwargs = _client_kwargs()

    assert kwargs == _bundled(default)
    for probe in (access, identity):
        assert probe.call_args.args[0] == default
        assert probe.call_args.kwargs["host"] == stack_cs.LOOPBACK_HOST


class _Root(BaseHTTPRequestHandler):
    status = 200
    body = b""

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: str | int) -> None:
        return None


@pytest.mark.parametrize(
    ("status", "body", "identified"),
    [
        (200, json.dumps(_QDRANT_ROOT).encode(), True),
        (200, json.dumps(_OTHER_SERVICE).encode(), False),
        (200, json.dumps({"title": "qdrant"}).encode(), False),
        (200, json.dumps({"title": "qdrant", "version": ""}).encode(), False),
        (200, json.dumps([_QDRANT_ROOT]).encode(), False),
        (200, b"<html>qdrant 1.19.1</html>", False),
        (401, json.dumps(_QDRANT_ROOT).encode(), False),
    ],
)
def test_only_qdrant_s_own_root_identifies_it(
    status: int, body: bytes, identified: bool
) -> None:
    from codebase_rag.stack.health import qdrant_identifies

    handler = type("Handler", (_Root,), {"status": status, "body": body})
    server = ThreadingHTTPServer((stack_cs.LOOPBACK_HOST, 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert qdrant_identifies(port, host=stack_cs.LOOPBACK_HOST) is identified
    finally:
        server.shutdown()
        server.server_close()


def test_the_choice_is_logged(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stack_on(open_qdrant.port, compose, monkeypatch)

    _, messages = _client_kwargs_logged("INFO")

    assert any(str(open_qdrant.port) in m for m in messages)


def test_a_configured_api_key_is_not_sent_to_the_bundled_qdrant(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: QDRANT_API_KEY belongs to the server QDRANT_URL names; with no
    # QDRANT_URL it is for nobody here, and the stack's Qdrant never gets it.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    monkeypatch.setattr(settings, "QDRANT_API_KEY", "for-another-server")

    assert _client_kwargs()["api_key"] is None


def test_without_the_stack_set_up_the_embedded_store_is_used(
    _isolate_cgr_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: a Qdrant answering on the port is not the bundled stack
    # unless `cgr daemon up` set one up here, and Docker is not even asked.
    _leave_db_path_unset(monkeypatch)
    _stack_on(open_qdrant.port, compose, monkeypatch)

    assert _client_kwargs() == _embedded()
    assert compose.calls == []


def test_a_stopped_stack_falls_back_to_the_embedded_store(
    stack_home: Path, compose: _Compose, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _serve_then_stop() as port:
        _stack_on(port, compose, monkeypatch)

    assert _client_kwargs() == _embedded()


def test_a_bundled_qdrant_that_wants_a_key_is_not_adopted(
    stack_home: Path,
    keyed_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: every write would fail against it with no key to send, so
    # the embedded store is kept, and the log says why.
    _stack_on(keyed_qdrant.port, compose, monkeypatch)

    kwargs, warnings = _client_kwargs_logged("WARNING")

    assert kwargs == _embedded()
    assert any("QDRANT_URL" in w for w in warnings)


def test_an_explicit_db_path_keeps_the_embedded_store_and_skips_the_probe(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Negative: a user who chose a folder keeps it.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    monkeypatch.setattr(settings, _DB_PATH_SETTING, str(tmp_path / "vectors"))

    assert _client_kwargs() == {"path": str(tmp_path / "vectors")}
    assert open_qdrant.paths == []
    assert compose.calls == []


def _settings_from(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """Settings as a fresh process builds them from its environment and .env."""
    for name in ("QDRANT_URL", "CGR_VECTOR_STORE_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    return AppConfig(_env_file=env_file)


@pytest.mark.parametrize("source", ["environment", ".env file"])
def test_an_explicit_default_valued_db_path_stays_embedded(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    # QDRANT_DB_PATH=./.qdrant_code_embeddings is a choice even though it is
    # the default value: the user asked for the embedded store there.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    env_file = tmp_path / ".env"
    if source == "environment":
        monkeypatch.setenv(_DB_PATH_SETTING, cs.QDRANT_DEFAULT_DB_PATH)
        env_file.write_text("")
    else:
        monkeypatch.delenv(_DB_PATH_SETTING, raising=False)
        env_file.write_text(f"{_DB_PATH_SETTING}={cs.QDRANT_DEFAULT_DB_PATH}\n")
    monkeypatch.setattr(vs, "settings", _settings_from(env_file, monkeypatch))

    assert _client_kwargs() == _embedded()
    assert open_qdrant.paths == []


def test_an_unset_db_path_still_follows_the_stack(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Negative: with QDRANT_DB_PATH in neither the environment nor .env,
    # the running stack's Qdrant takes the embeddings.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    monkeypatch.delenv(_DB_PATH_SETTING, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(vs, "settings", _settings_from(env_file, monkeypatch))

    assert _client_kwargs() == _bundled(open_qdrant.port)


def test_an_explicit_url_still_wins(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: QDRANT_URL names the server; the stack is not consulted.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    monkeypatch.setattr(settings, "QDRANT_URL", "http://qdrant.internal:6333")

    assert _client_kwargs()["url"] == "http://qdrant.internal:6333"
    assert open_qdrant.paths == []
    assert compose.calls == []


class _serve_then_stop:
    def __enter__(self) -> int:
        self._gen = _serve()
        return next(self._gen).port

    def __exit__(self, *exc: BaseException | type[BaseException] | None) -> None:
        self._gen.close()


@pytest.fixture
def fresh_embedding_cache() -> Generator[None, None, None]:
    from codebase_rag import embedder

    embedder.clear_embedding_cache()
    yield
    embedder.clear_embedding_cache()


def _cache_path() -> Path:
    from codebase_rag import embedder

    path = embedder.get_embedding_cache()._path
    assert path is not None
    return path


def test_the_embedding_cache_leaves_the_repository_with_the_vectors(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fresh_embedding_cache: None,
) -> None:
    # The cache holds the vectors too; left at the cwd-relative default it
    # still dropped a hidden folder into every indexed repository.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    _stack_on(open_qdrant.port, compose, monkeypatch)

    path = _cache_path()

    assert path.parent == stack_home
    assert not (repo / cs.QDRANT_DEFAULT_DB_PATH).exists()


def test_without_the_stack_the_cache_stays_beside_the_embedded_store(
    _isolate_cgr_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_embedding_cache: None,
) -> None:
    # Negative: the embedded store and its cache keep sharing a folder.
    _leave_db_path_unset(monkeypatch)

    assert _cache_path() == Path(cs.QDRANT_DEFAULT_DB_PATH) / (
        cs.EMBEDDING_CACHE_FILENAME
    )


def test_an_explicit_url_keeps_the_cache_where_it_was(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    fresh_embedding_cache: None,
) -> None:
    # Negative: only the bundled stack's cache moves.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    monkeypatch.setattr(settings, "QDRANT_URL", "http://qdrant.internal:6333")

    assert _cache_path().parent == Path(cs.QDRANT_DEFAULT_DB_PATH)


def test_the_stack_is_probed_once_for_the_client_and_the_cache(
    stack_home: Path,
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    fresh_embedding_cache: None,
) -> None:
    from codebase_rag import stack

    _stack_on(open_qdrant.port, compose, monkeypatch)
    probe = MagicMock(wraps=stack.bundled_qdrant_url)
    monkeypatch.setattr(stack, "bundled_qdrant_url", probe)

    _client_kwargs()
    _cache_path()

    assert probe.call_count == 1
