"""What the embedded-graph default changes for a first run (issue #386).

The point of the default is that nothing else has to be running: no Docker
stack is started, `cgr doctor` does not fail a setup for lacking Docker, and
the graph is found and opened under CGR_HOME. Selecting Memgraph without its
driver installed says what to install instead of failing on an import.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger

from codebase_rag import cli
from codebase_rag import exceptions as ex
from codebase_rag.config import AppConfig, settings
from codebase_rag.constants import (
    GRAPH_BACKEND_SETTING,
    MEMGRAPH_CONNECTION_SETTINGS,
    SETTING_QDRANT_DB_PATH,
    VectorStoreBackend,
)
from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, DIALECT_SQLITE, get_dialect
from codebase_rag.mgclient_compat import _absent_driver
from codebase_rag.schemas import HealthCheckResult
from codebase_rag.services import graph_service
from codebase_rag.services.graph_service import GraphUnavailableError, MemgraphIngestor
from codebase_rag.stack import manager as manager_module
from codebase_rag.stack.constants import SERVICE_MEMGRAPH, SERVICE_QDRANT
from codebase_rag.stack.manager import StackError, StackManager
from codebase_rag.tools import health_checker
from codebase_rag.tools.health_checker import HealthChecker
from codebase_rag.types_defs import JsonValue

_PASS = HealthCheckResult(name="ok", passed=True, message="ok")
# Bound at import, before conftest's autouse fixture swaps the function for
# a mock in every test.
_maybe_start_stack = cli._maybe_start_stack


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_SQLITE)


def test_no_stack_is_started_for_the_embedded_graph(embedded) -> None:
    with patch.object(cli, "StackManager") as manager:
        manager.return_value.serves_vectors.return_value = False
        _maybe_start_stack()
    manager.return_value.ensure_running.assert_not_called()


def test_the_stack_starts_when_it_holds_the_vectors(embedded) -> None:
    """A QDRANT_URL naming the bundled Qdrant, or a stack set up here with
    no vector location chosen, still needs the stack (review of #3377)."""
    with patch.object(cli, "StackManager") as manager:
        manager.return_value.serves_vectors.return_value = True
        manager.return_value.status.return_value.state = None
        _maybe_start_stack()
    manager.return_value.ensure_running.assert_called_once()
    # Only the Qdrant: its Memgraph would need a driver this install lacks.
    manager.assert_called_once_with(services=(SERVICE_QDRANT,))


def test_the_default_vector_store_needs_no_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compose file left in CGR_HOME by a Memgraph-era `cgr start` does not
    send the default install's vectors to Docker: they go to a file."""
    monkeypatch.delenv("CGR_VECTOR_STORE_BACKEND", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(manager_module, "settings", AppConfig(CGR_HOME=tmp_path))
    manager = StackManager(home=tmp_path, services=(SERVICE_QDRANT,))
    manager.compose_file.write_text("services: {}\n")
    assert manager.serves_vectors() is False


@pytest.mark.parametrize("via", ["flag", "setting"])
def test_no_stack_is_started_for_the_embedded_graph_without_embeddings(
    embedded, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    """A stack set up here holds the vectors, but `--no-embeddings` (or
    CGR_SKIP_EMBEDDINGS) writes none, so Docker is not even asked."""
    monkeypatch.setattr(settings, "SKIP_EMBEDDINGS", via == "setting")
    with patch.object(cli, "StackManager") as manager:
        manager.return_value.serves_vectors.return_value = True
        _maybe_start_stack(skip_embeddings=via == "flag")
    manager.assert_not_called()


@pytest.mark.parametrize(
    ("skip_embeddings", "backend"),
    [(True, VectorStoreBackend.QDRANT), (False, VectorStoreBackend.SQLITE)],
    ids=["no-embeddings", "sqlite-vectors"],
)
def test_memgraph_without_qdrant_vectors_starts_only_memgraph(
    monkeypatch: pytest.MonkeyPatch,
    skip_embeddings: bool,
    backend: VectorStoreBackend,
) -> None:
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_MEMGRAPH)
    monkeypatch.setattr(settings, "VECTOR_STORE_BACKEND", backend)
    with patch.object(cli, "StackManager") as manager:
        manager.return_value.status.return_value.state = None
        _maybe_start_stack(skip_embeddings=skip_embeddings)
    manager.assert_called_once_with(services=(SERVICE_MEMGRAPH,))
    manager.return_value.ensure_running.assert_called_once()


@pytest.mark.parametrize(
    ("qdrant_url", "compose_exists", "expected"),
    [
        # The stack's own Qdrant, named by URL.
        ("http://127.0.0.1:6333", False, True),
        # Another server: the stack holds no vectors.
        ("http://qdrant.example.invalid:6333", True, False),
        # No location chosen: the stack's Qdrant once it is set up here.
        (None, True, True),
        (None, False, False),
    ],
)
def test_serves_vectors_follows_the_vector_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qdrant_url: str | None,
    compose_exists: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(settings, "QDRANT_URL", qdrant_url)
    # conftest points QDRANT_DB_PATH at a temp folder, which reads as chosen.
    monkeypatch.setattr(
        settings,
        "__pydantic_fields_set__",
        settings.model_fields_set - {SETTING_QDRANT_DB_PATH},
    )
    # Without Docker the default port stands; asking Compose is tested below.
    monkeypatch.setattr(manager_module.shutil, "which", lambda _: None)
    manager = StackManager(home=tmp_path)
    if compose_exists:
        manager.compose_file.write_text("services: {}\n")
    assert manager.serves_vectors() is expected


def _published_on(port: str) -> dict[str, JsonValue]:
    """`docker compose config` for a stack publishing Qdrant on `port`."""
    entry: dict[str, JsonValue] = {"target": 6333, "published": port}
    return {"services": {SERVICE_QDRANT: {"ports": [entry]}}}


@pytest.mark.parametrize(
    ("qdrant_url", "published", "expected"),
    [
        # Greptile on #3377: Compose publishes the stack's Qdrant on 16333,
        # and QDRANT_URL says so; compared with the default 6333 instead,
        # the stack was never started and every embedding write failed.
        ("http://localhost:16333", "16333", True),
        ("http://localhost:6333", "16333", False),
        ("http://localhost:6333", "6333", True),
    ],
)
def test_serves_vectors_asks_compose_where_qdrant_is_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qdrant_url: str,
    published: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(settings, "QDRANT_URL", qdrant_url)
    monkeypatch.setattr(settings, "QDRANT_API_KEY", None)
    monkeypatch.setattr(manager_module.shutil, "which", lambda _: "/usr/bin/docker")
    manager = StackManager(home=tmp_path)
    manager.compose_file.write_text("services: {}\n")
    with patch.object(
        manager, "_resolved_config", return_value=_published_on(published)
    ):
        assert manager.serves_vectors() is expected


@pytest.mark.parametrize(
    "failure",
    [
        StackError("no config"),
        # Greptile on #3377: a Compose slower than its status timeout must
        # not stop `cgr start`.
        subprocess.TimeoutExpired(["docker", "compose", "config"], 10),
        OSError("docker vanished"),
    ],
)
def test_serves_vectors_keeps_the_default_when_compose_cannot_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    monkeypatch.setattr(settings, "QDRANT_URL", "http://localhost:6333")
    monkeypatch.setattr(manager_module.shutil, "which", lambda _: "/usr/bin/docker")
    manager = StackManager(home=tmp_path)
    manager.compose_file.write_text("services: {}\n")
    with patch.object(manager, "_resolved_config", side_effect=failure):
        assert manager.serves_vectors() is True


def test_serves_vectors_does_not_ask_compose_about_a_remote_qdrant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stack can publish only on this machine's addresses.
    monkeypatch.setattr(settings, "QDRANT_URL", "http://qdrant.example.invalid:16333")
    monkeypatch.setattr(manager_module.shutil, "which", lambda _: "/usr/bin/docker")
    manager = StackManager(home=tmp_path)
    manager.compose_file.write_text("services: {}\n")
    with patch.object(manager, "_resolved_config") as resolved:
        assert manager.serves_vectors() is False
    resolved.assert_not_called()


def test_the_stack_is_still_started_for_memgraph(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_MEMGRAPH)
    with patch.object(cli, "StackManager") as manager:
        _maybe_start_stack()
    manager.assert_called_once_with()


def test_doctor_does_not_require_docker_for_the_embedded_graph(
    embedded, monkeypatch
) -> None:
    def docker_missing(self: HealthChecker) -> HealthCheckResult:
        raise AssertionError("docker was checked")

    monkeypatch.setattr(HealthChecker, "check_docker", docker_missing)
    monkeypatch.setattr(HealthChecker, "check_model_roles", lambda self: [])
    monkeypatch.setattr(
        HealthChecker, "check_external_tool", lambda self, name, cmd=None: _PASS
    )
    results = HealthChecker().run_all_checks()
    graph = results[0]
    assert graph.passed, graph
    assert str(settings.graph_db_file) in graph.message


def test_doctor_names_the_embedded_graph(embedded) -> None:
    assert health_checker._backend_engine_name() == "Embedded graph (SQLite)"
    assert health_checker._backend_endpoint() == str(settings.graph_db_file)


def test_the_default_graph_opens_under_cgr_home(embedded) -> None:
    with MemgraphIngestor(host="unused", port=0) as ingestor:
        ingestor.ensure_constraints()
    assert settings.graph_db_file.is_file()
    assert settings.graph_db_file.parent == settings.CGR_HOME


def test_an_unwritable_graph_path_is_one_clear_error(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    ingestor = MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(blocker / "graph.sqlite"),
    )
    with pytest.raises(GraphUnavailableError, match="SQLITE_GRAPH_PATH"):
        ingestor.__enter__()


def test_memgraph_without_its_driver_says_what_to_install(monkeypatch) -> None:
    monkeypatch.setattr(graph_service, "MGCLIENT_AVAILABLE", False)
    ingestor = MemgraphIngestor(
        host="localhost", port=7687, dialect=get_dialect(DIALECT_MEMGRAPH)
    )
    with pytest.raises(GraphUnavailableError) as raised:
        ingestor.__enter__()
    assert str(raised.value) == ex.MGCLIENT_MISSING


def test_the_absent_driver_fails_where_a_connection_would() -> None:
    # Callers catch mgclient.Error around connect; the stand-in raises one.
    absent = _absent_driver()
    with pytest.raises(absent.Error, match="memgraph"):
        absent.connect(host="localhost", port=7687)


class TestMemgraphSetWithoutBackend:
    """An install upgraded in place from when Memgraph was the default keeps
    its MEMGRAPH_* settings and never set GRAPH_BACKEND. Its graph is still in
    Memgraph while cgr now opens an empty file, so it is told so, once."""

    @pytest.fixture
    def notes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Iterator[list[str]]:
        for name in (GRAPH_BACKEND_SETTING, *MEMGRAPH_CONNECTION_SETTINGS):
            monkeypatch.delenv(name, raising=False)
        # AppConfig reads `.env` from the working directory.
        monkeypatch.chdir(tmp_path)
        graph_service._note_memgraph_set_without_backend.cache_clear()
        messages: list[str] = []
        sink = logger.add(
            lambda message: messages.append(message.record["message"]),
            level="WARNING",
        )
        yield messages
        logger.remove(sink)
        graph_service._note_memgraph_set_without_backend.cache_clear()

    @staticmethod
    def _open(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sqlite_path: str | None = None
    ) -> None:
        monkeypatch.setattr(graph_service, "settings", AppConfig(CGR_HOME=tmp_path))
        MemgraphIngestor(host="unused", port=0, sqlite_path=sqlite_path)

    def test_memgraph_settings_without_a_backend_are_noted_once(
        self, notes: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MEMGRAPH_HOST", "db.internal")
        self._open(monkeypatch, tmp_path)
        self._open(monkeypatch, tmp_path)
        assert len(notes) == 1
        assert str(tmp_path / "graph.sqlite") in notes[0]
        assert "GRAPH_BACKEND=memgraph" in notes[0]

    @pytest.mark.parametrize(
        "env",
        [
            # Nothing chosen: a first install, nothing to warn about.
            {},
            # Either explicit choice settles it.
            {"MEMGRAPH_HOST": "db.internal", "GRAPH_BACKEND": "sqlite"},
            {"MEMGRAPH_PORT": "7688", "GRAPH_BACKEND": "SQLite"},
        ],
    )
    def test_a_settled_choice_is_not_noted(
        self,
        notes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env: dict[str, str],
    ) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        self._open(monkeypatch, tmp_path)
        assert notes == []

    def test_a_blank_host_copied_from_the_example_is_not_a_setting(
        self, notes: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / ".env").write_text("MEMGRAPH_HOST=\n", encoding="utf-8")
        self._open(monkeypatch, tmp_path)
        assert notes == []

    def test_a_graph_file_chosen_by_the_caller_is_not_noted(
        self, notes: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MEMGRAPH_HOST", "db.internal")
        self._open(monkeypatch, tmp_path, sqlite_path=str(tmp_path / "own.sqlite"))
        assert notes == []
