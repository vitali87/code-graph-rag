"""What the embedded-graph default changes for a first run (issue #386).

The point of the default is that nothing else has to be running: no Docker
stack is started, `cgr doctor` does not fail a setup for lacking Docker, and
the graph is found and opened under CGR_HOME. Selecting Memgraph without its
driver installed says what to install instead of failing on an import.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import cli
from codebase_rag import exceptions as ex
from codebase_rag.config import settings
from codebase_rag.constants import SETTING_QDRANT_DB_PATH
from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, DIALECT_SQLITE, get_dialect
from codebase_rag.mgclient_compat import _absent_driver
from codebase_rag.schemas import HealthCheckResult
from codebase_rag.services import graph_service
from codebase_rag.services.graph_service import GraphUnavailableError, MemgraphIngestor
from codebase_rag.stack import manager as manager_module
from codebase_rag.stack.constants import SERVICE_QDRANT
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
