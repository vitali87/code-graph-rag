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


class TestPublicPortWarning:
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

    def test_a_literal_loopback_mapping_is_quiet(self, tmp_path: Path) -> None:
        # The documented remediation is a literal '127.0.0.1:' prefix. Warning
        # on every `daemon up` after the user followed it trains them to
        # ignore the warning.
        manager = self._manager(
            tmp_path,
            'services:\n  memgraph:\n    ports:\n      - "127.0.0.1:7687:7687"\n',
        )

        assert self._warnings(manager) == []

    def test_one_fixed_service_does_not_vouch_for_another(self, tmp_path: Path) -> None:
        # A substring check over the whole file let a single corrected mapping
        # silence the warning while another service stayed bound on 0.0.0.0.
        manager = self._manager(
            tmp_path,
            "services:\n"
            "  memgraph:\n"
            '    ports:\n      - "${CGR_STACK_BIND_HOST:-127.0.0.1}:7687:7687"\n'
            "  qdrant:\n"
            '    ports:\n      - "6333:6333"\n',
        )

        messages = self._warnings(manager)
        assert any("qdrant" in message for message in messages), messages

    def test_a_comment_does_not_silence_the_warning(self, tmp_path: Path) -> None:
        manager = self._manager(
            tmp_path,
            "# CGR_STACK_BIND_HOST is how you would fix this\n"
            'services:\n  qdrant:\n    ports:\n      - "6333:6333"\n',
        )

        assert any("ALL interfaces" in message for message in self._warnings(manager))

    def test_an_explicit_wildcard_host_still_warns(self, tmp_path: Path) -> None:
        manager = self._manager(
            tmp_path,
            'services:\n  qdrant:\n    ports:\n      - "0.0.0.0:6333:6333"\n',
        )

        assert any("ALL interfaces" in message for message in self._warnings(manager))

    def test_an_ipv6_wildcard_mapping_warns(self, tmp_path: Path) -> None:
        # Compose brackets an IPv6 host so its colons are not field separators;
        # `[::]` is the IPv6 equivalent of 0.0.0.0.
        manager = self._manager(
            tmp_path,
            'services:\n  qdrant:\n    ports:\n      - "[::]:6333:6333"\n',
        )

        assert any("ALL interfaces" in message for message in self._warnings(manager))

    def test_an_ipv6_loopback_mapping_is_quiet(self, tmp_path: Path) -> None:
        manager = self._manager(
            tmp_path,
            'services:\n  qdrant:\n    ports:\n      - "[::1]:6333:6333"\n',
        )

        assert self._warnings(manager) == []

    def test_long_form_host_ip_is_honoured(self, tmp_path: Path) -> None:
        manager = self._manager(
            tmp_path,
            "services:\n  qdrant:\n    ports:\n"
            "      - target: 6333\n        published: 6333\n"
            '        host_ip: "127.0.0.1"\n',
        )

        assert self._warnings(manager) == []

    def test_a_user_edited_file_is_not_clobbered(self, tmp_path: Path) -> None:
        rendered = 'services:\n  memgraph:\n    ports:\n      - "7687:7687"\n'
        manager = self._manager(tmp_path, rendered)

        manager.ensure_compose_file()

        assert manager.compose_file.read_text(encoding="utf-8") == rendered


@pytest.mark.parametrize("service", ["memgraph", "lab", "qdrant"])
def test_each_service_is_covered(service: str) -> None:
    assert any(name == service for name, _mapping in _published_ports())


SECURITY_DOC = REPO_ROOT / "docs" / "architecture" / "security.md"


def test_the_security_guide_describes_the_loopback_default() -> None:
    # The guide kept claiming the ports were "currently network-reachable" and
    # pointed at #1012 as still tracking a fix, for months after #1012 shipped
    # the loopback bind (issue #1372, found in an external field-test review).
    # A security document that MISSTATES exposure in either direction is worse
    # than a stale one: understating it is how a reader leaves the graph open.
    #
    # Rejecting the old phrase alone is not enough -- a guide claiming the
    # services bind everywhere and are safe would pass that. Assert the actual
    # security claims this file exists to keep true.
    doc = SECURITY_DOC.read_text(encoding="utf-8")
    assert "network-reachable" not in doc, (
        "security.md still describes the pre-#1012 exposure; a newly rendered "
        f"compose file now binds every published port to {LOOPBACK}"
    )
    assert LOOPBACK in doc, f"security.md must state the {LOOPBACK} default"
    assert cs.COMPOSE_BIND_HOST_VAR in doc, (
        f"security.md must name {cs.COMPOSE_BIND_HOST_VAR}, since widening the "
        "bind is one of the two ways to re-create the exposure"
    )
    assert "UNAUTHENTICATED" in doc, (
        "security.md must say the services carry no credential, or a wider "
        "bind reads as a routine configuration change"
    )
    assert "never overwritten" in doc, (
        "security.md must warn that a pre-fix ~/.cgr/docker-compose.yaml keeps "
        "its bare mappings; StackManager warns but does not migrate it"
    )
    # ORDER is the whole point, not the presence of the command: a guide saying
    # "delete the file, then run cgr daemon down" contains the same words and
    # leaves the reader exposed. Compare positions rather than pinning the
    # sentence, so rewording the guide does not break the test but reversing the
    # steps does.
    stop_at = doc.find("cgr daemon down")
    delete_at = doc.find("delete the file")
    assert stop_at != -1, (
        "the remediation must stop the stack first: ensure_running() returns "
        "early on a healthy stack, so deleting the compose file while it is up "
        "neither re-renders the file nor replaces the exposed containers"
    )
    assert delete_at != -1, "the remediation must tell the reader to delete the file"
    assert stop_at < delete_at, (
        "security.md tells the reader to delete the compose file before "
        "stopping the stack, which does nothing: the running containers keep "
        "their old bindings and 'cgr daemon up' returns early without "
        "re-rendering"
    )
