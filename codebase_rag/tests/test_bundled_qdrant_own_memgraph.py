"""The stack's Qdrant takes the embeddings only of the stack's own Memgraph.

With QDRANT_URL unset, the Qdrant of `cgr daemon up` was used whenever it
ran, whatever Memgraph the app wrote to. A scratch graph on MEMGRAPH_PORT=7704
then put its vectors, keyed by its own node ids, into the stack graph's
collection, and `cgr start --clean` on it (an empty graph: no confirmation)
dropped the whole collection the stack graph's projects use (issue #2878).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.config import settings
from codebase_rag.stack import constants as stack_cs
from codebase_rag.tests.test_bundled_qdrant_default import (
    _bundled,
    _client_kwargs,
    _client_kwargs_logged,
    _Compose,
    _embedded,
    _fresh_client,
    _Server,
    _stack_on,
    compose,
    open_qdrant,
    stack_home,
)

__all__ = ["_fresh_client", "compose", "open_qdrant", "stack_home"]

_STACK_BOLT = (stack_cs.LOOPBACK_HOST, 7687)


@pytest.fixture
def stack_memgraph_on_7687(compose: _Compose) -> None:
    compose.memgraph_published = [_STACK_BOLT]


@pytest.mark.usefixtures("stack_home", "stack_memgraph_on_7687")
@pytest.mark.parametrize(
    ("host", "port"),
    [
        # The issue's scratch graph beside the stack.
        ("localhost", 7704),
        # The stack's port, on another machine.
        ("10.255.255.1", 7687),
    ],
)
def test_another_memgraph_keeps_its_embeddings_out_of_the_stack(
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    port: int,
) -> None:
    _stack_on(open_qdrant.port, compose, monkeypatch)
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", host)
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", port)

    kwargs, messages = _client_kwargs_logged("INFO")

    assert kwargs == _embedded()
    # Nothing was even sent to the stack's Qdrant.
    assert open_qdrant.paths == []
    said = " ".join(messages)
    assert f"{host}:{port}" in said, messages
    assert "QDRANT_URL" in said, messages


@pytest.mark.usefixtures("stack_home")
def test_a_stack_memgraph_published_nowhere_is_not_the_app_graph(
    open_qdrant: _Server, compose: _Compose, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack_on(open_qdrant.port, compose, monkeypatch)
    compose.memgraph_published = []
    assert _client_kwargs() == _embedded()


@pytest.mark.usefixtures("stack_home")
def test_a_stopped_stack_memgraph_is_not_the_app_graph(
    open_qdrant: _Server, compose: _Compose, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack_on(open_qdrant.port, compose, monkeypatch)
    compose.memgraph_running = False
    assert _client_kwargs() == _embedded()


@pytest.mark.usefixtures("stack_home", "stack_memgraph_on_7687")
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_the_stack_memgraph_still_shares_the_stack_qdrant(
    open_qdrant: _Server,
    compose: _Compose,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    # Negative: the app writing to the stack's own Memgraph is the case the
    # bundled Qdrant exists for.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", host)
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", 7687)
    assert _client_kwargs() == _bundled(open_qdrant.port)


@pytest.mark.usefixtures("stack_home")
def test_a_wildcard_bind_serves_the_loopback_address(
    open_qdrant: _Server, compose: _Compose, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: CGR_STACK_BIND_HOST=0.0.0.0 publishes on every address.
    _stack_on(open_qdrant.port, compose, monkeypatch)
    compose.memgraph_published = [("0.0.0.0", 7687)]  # noqa: S104 - a published bind
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", 7687)
    assert _client_kwargs() == _bundled(open_qdrant.port)


def test_the_compose_file_takes_the_port_from_the_caller() -> None:
    # Why the check asks Docker: the port mapping interpolates MEMGRAPH_PORT,
    # so Compose's resolved configuration follows the caller's own setting
    # and would always agree with it; only the running container's
    # publishers say where the stack's Memgraph is.
    compose_file = Path(__file__).resolve().parents[1] / "docker-compose.yaml"
    assert "${MEMGRAPH_PORT:-7687}:7687" in compose_file.read_text(encoding="utf-8")
