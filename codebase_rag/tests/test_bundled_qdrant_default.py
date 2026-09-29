"""Issue #2355: with the stack up, embeddings go to the stack's Qdrant.

`cgr daemon up` starts a Qdrant server, but with QDRANT_URL unset the vector
store opened an embedded Qdrant at the cwd-relative QDRANT_DB_PATH, so the
vectors landed in a hidden folder inside the indexed repository and the
stack's Qdrant stayed empty.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import vector_store as vs
from codebase_rag.config import settings
from codebase_rag.stack import constants as stack_cs


class _FakeQdrant(BaseHTTPRequestHandler):
    refuse_anonymous = False

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
        if self.path.startswith(stack_cs.QDRANT_READY_PATH):
            status = 200
        elif self.refuse_anonymous:
            status = 401
        else:
            status = 200
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        return None


def _serve(refuse_anonymous: bool) -> Generator[int, None, None]:
    handler = type("Handler", (_FakeQdrant,), {"refuse_anonymous": refuse_anonymous})
    server = ThreadingHTTPServer((stack_cs.LOOPBACK_HOST, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def open_qdrant() -> Generator[int, None, None]:
    yield from _serve(refuse_anonymous=False)


@pytest.fixture
def keyed_qdrant() -> Generator[int, None, None]:
    yield from _serve(refuse_anonymous=True)


@pytest.fixture
def stack_home(_isolate_cgr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # `cgr daemon up` writes the compose file here; its presence is what
    # says the bundled stack was set up on this machine.
    (_isolate_cgr_home / stack_cs.COMPOSE_FILENAME).write_text("services: {}\n")
    monkeypatch.setattr(settings, "QDRANT_DB_PATH", cs.QDRANT_DEFAULT_DB_PATH)
    return _isolate_cgr_home


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


def test_the_running_stack_qdrant_receives_the_embeddings(
    stack_home: Path, open_qdrant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))

    kwargs = _client_kwargs()

    assert kwargs == {
        "url": f"http://{stack_cs.LOOPBACK_HOST}:{open_qdrant}",
        "api_key": None,
    }


def test_the_port_is_read_from_the_env_file_beside_the_compose_file(
    stack_home: Path, open_qdrant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, raising=False)
    (stack_home / stack_cs.COMPOSE_DOTENV_FILENAME).write_text(
        f"{stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR}={open_qdrant}\n"
    )

    assert _client_kwargs()["url"] == f"http://{stack_cs.LOOPBACK_HOST}:{open_qdrant}"


def test_the_choice_is_logged(
    stack_home: Path, open_qdrant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))
    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO", format="{message}")
    try:
        _client_kwargs()
    finally:
        logger.remove(sink)

    assert any(str(open_qdrant) in m for m in messages)


def test_a_configured_api_key_is_not_sent_to_the_bundled_qdrant(
    stack_home: Path, open_qdrant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: QDRANT_API_KEY belongs to the server QDRANT_URL names; with no
    # QDRANT_URL it is for nobody here, and the stack's Qdrant never gets it.
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))
    monkeypatch.setattr(settings, "QDRANT_API_KEY", "for-another-server")

    assert _client_kwargs()["api_key"] is None


def test_without_the_stack_set_up_the_embedded_store_is_used(
    _isolate_cgr_home: Path, open_qdrant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: a Qdrant answering on the port is not the bundled stack
    # unless `cgr daemon up` set one up here.
    monkeypatch.setattr(settings, "QDRANT_DB_PATH", cs.QDRANT_DEFAULT_DB_PATH)
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))

    assert _client_kwargs() == {"path": cs.QDRANT_DEFAULT_DB_PATH}


def test_a_stopped_stack_falls_back_to_the_embedded_store(
    stack_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _serve_then_stop() as port:
        monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(port))

    assert _client_kwargs() == {"path": cs.QDRANT_DEFAULT_DB_PATH}


def test_a_bundled_qdrant_that_wants_a_key_is_not_adopted(
    stack_home: Path, keyed_qdrant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: every write would fail against it with no key to send, so
    # the embedded store is kept, and the log says why.
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(keyed_qdrant))
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        kwargs = _client_kwargs()
    finally:
        logger.remove(sink)

    assert kwargs == {"path": cs.QDRANT_DEFAULT_DB_PATH}
    assert any("QDRANT_URL" in w for w in warnings)


def test_an_explicit_db_path_keeps_the_embedded_store_and_skips_the_probe(
    stack_home: Path, open_qdrant: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Negative: a user who chose a folder keeps it.
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))
    monkeypatch.setattr(settings, "QDRANT_DB_PATH", str(tmp_path / "vectors"))

    assert _client_kwargs() == {"path": str(tmp_path / "vectors")}


def test_an_explicit_url_still_wins(
    stack_home: Path, open_qdrant: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))
    monkeypatch.setattr(settings, "QDRANT_URL", "http://qdrant.internal:6333")

    assert _client_kwargs()["url"] == "http://qdrant.internal:6333"


class _serve_then_stop:
    def __enter__(self) -> int:
        self._gen = _serve(refuse_anonymous=False)
        return next(self._gen)

    def __exit__(self, *exc: object) -> None:
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
    open_qdrant: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fresh_embedding_cache: None,
) -> None:
    # The cache holds the vectors too; left at the cwd-relative default it
    # still dropped a hidden folder into every indexed repository.
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))

    path = _cache_path()

    assert path.parent == stack_home
    assert not (repo / cs.QDRANT_DEFAULT_DB_PATH).exists()


def test_without_the_stack_the_cache_stays_beside_the_embedded_store(
    _isolate_cgr_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_embedding_cache: None,
) -> None:
    # Negative: the embedded store and its cache keep sharing a folder.
    monkeypatch.setattr(settings, "QDRANT_DB_PATH", cs.QDRANT_DEFAULT_DB_PATH)

    assert _cache_path() == Path(cs.QDRANT_DEFAULT_DB_PATH) / (
        cs.EMBEDDING_CACHE_FILENAME
    )


def test_an_explicit_url_keeps_the_cache_where_it_was(
    stack_home: Path,
    open_qdrant: int,
    monkeypatch: pytest.MonkeyPatch,
    fresh_embedding_cache: None,
) -> None:
    # Negative: only the bundled stack's cache moves.
    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))
    monkeypatch.setattr(settings, "QDRANT_URL", "http://qdrant.internal:6333")

    assert _cache_path().parent == Path(cs.QDRANT_DEFAULT_DB_PATH)


def test_the_stack_is_probed_once_for_the_client_and_the_cache(
    stack_home: Path,
    open_qdrant: int,
    monkeypatch: pytest.MonkeyPatch,
    fresh_embedding_cache: None,
) -> None:
    from codebase_rag import stack

    monkeypatch.setenv(stack_cs.COMPOSE_QDRANT_HTTP_PORT_VAR, str(open_qdrant))
    probe = MagicMock(wraps=stack.bundled_qdrant_url)
    monkeypatch.setattr(stack, "bundled_qdrant_url", probe)

    _client_kwargs()
    _cache_path()

    assert probe.call_count == 1
