"""The daemon stack must not publish its ports on every interface (#1012).

Memgraph Bolt, Memgraph Lab, and Qdrant are unauthenticated. A bare
``host:container`` mapping binds to 0.0.0.0, so `cgr daemon up` put the whole
code graph on the local network -- the same exposure class as #808, fixed there
for the MCP HTTP server by defaulting to a loopback bind.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from loguru import logger

from codebase_rag.stack import constants as cs
from codebase_rag.stack.manager import StackManager

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "codebase_rag" / "docker-compose.yaml"

LOOPBACK = "127.0.0.1"


def _published_ports() -> list[tuple[str, str]]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return [
        (service, mapping)
        for service, spec in compose["services"].items()
        for mapping in spec.get("ports", [])
    ]


def test_every_published_port_is_bound_to_a_host_address() -> None:
    unbound = [
        (service, mapping)
        for service, mapping in _published_ports()
        if mapping.count(":") < 2
    ]

    assert unbound == [], (
        f"{unbound} publish without a host address, which Docker binds to "
        "0.0.0.0, exposing unauthenticated services to the whole network"
    )


def test_every_published_port_defaults_to_loopback() -> None:
    for service, mapping in _published_ports():
        assert mapping.startswith(f"${{{cs.COMPOSE_BIND_HOST_VAR}:-{LOOPBACK}}}:"), (
            f"{service} publishes {mapping!r}, which does not default to {LOOPBACK}"
        )


def test_the_bind_host_is_overridable() -> None:
    # A user who deliberately wants the stack reachable needs a documented way
    # to say so, rather than editing the rendered file.
    assert all(
        cs.COMPOSE_BIND_HOST_VAR in mapping for _service, mapping in _published_ports()
    )


def test_ports_are_still_published_at_their_documented_numbers() -> None:
    mappings = [mapping for _service, mapping in _published_ports()]
    container_ports = {mapping.rsplit(":", 1)[-1] for mapping in mappings}

    assert container_ports == {"7687", "7444", "3000", "6333", "6334"}


class TestStaleComposeWarning:
    def _manager(self, tmp_path: Path, rendered: str) -> StackManager:
        home = tmp_path / "cgr_home"
        home.mkdir()
        (home / cs.COMPOSE_FILENAME).write_text(rendered, encoding="utf-8")
        return StackManager(home=home, package_compose=COMPOSE_PATH)

    def _warnings(self, manager: StackManager) -> list[str]:
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING")
        try:
            manager.ensure_compose_file()
        finally:
            logger.remove(sink_id)
        return messages

    def test_a_pre_fix_compose_file_warns(self, tmp_path: Path) -> None:
        # The file is rendered once and never overwritten, so an existing
        # install keeps the exposed mapping and would never see the fix.
        manager = self._manager(
            tmp_path,
            'services:\n  memgraph:\n    ports:\n      - "7687:7687"\n',
        )

        assert any("ALL interfaces" in message for message in self._warnings(manager))

    def test_a_current_compose_file_is_quiet(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path, COMPOSE_PATH.read_text(encoding="utf-8"))

        assert self._warnings(manager) == []

    def test_a_user_edited_file_is_not_clobbered(self, tmp_path: Path) -> None:
        rendered = 'services:\n  memgraph:\n    ports:\n      - "7687:7687"\n'
        manager = self._manager(tmp_path, rendered)

        manager.ensure_compose_file()

        assert manager.compose_file.read_text(encoding="utf-8") == rendered


@pytest.mark.parametrize("service", ["memgraph", "lab", "qdrant"])
def test_each_service_is_covered(service: str) -> None:
    assert any(name == service for name, _mapping in _published_ports())
