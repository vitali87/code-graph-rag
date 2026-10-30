"""`host_site_scoped` puts the host's edge site and resolution back when a
collaborator's pass returns, so a nested pass cannot leak its last node into
the edges its caller emits next."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.edge_site import host_site_scoped


def _call_nodes() -> list[Node]:
    parsers, _ = load_parsers()
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(b"outer()\ninner()\n")
    return [stmt.children[0] for stmt in tree.root_node.children]


@dataclass
class _Host:
    site_node: Node | None = None
    resolution: str = "exact"


@dataclass
class _Collaborator:
    _host: _Host
    seen: list[tuple[Node | None, str]] = field(default_factory=list)

    @host_site_scoped
    def nested_pass(self, node: Node, resolution: str) -> str:
        self._host.site_node = node
        self._host.resolution = resolution
        self.seen.append((self._host.site_node, self._host.resolution))
        return node.type

    @host_site_scoped
    def failing_pass(self, node: Node) -> None:
        self._host.site_node = node
        self._host.resolution = "guessed"
        raise ValueError(node.type)


def test_the_site_set_by_a_pass_is_restored_when_it_returns() -> None:
    outer, inner = _call_nodes()
    host = _Host(site_node=outer, resolution="exact")
    collaborator = _Collaborator(host)

    assert collaborator.nested_pass(inner, "guessed") == "call"

    assert collaborator.seen == [(inner, "guessed")]
    assert host.site_node is outer
    assert host.resolution == "exact"


def test_the_site_is_restored_when_the_pass_raises() -> None:
    outer, inner = _call_nodes()
    host = _Host(site_node=outer)

    with pytest.raises(ValueError, match="call"):
        _Collaborator(host).failing_pass(inner)

    assert host.site_node is outer
    assert host.resolution == "exact"


def test_an_unset_site_stays_unset() -> None:
    _, inner = _call_nodes()
    host = _Host()
    _Collaborator(host).nested_pass(inner, "guessed")
    assert host.site_node is None


def test_the_wrapped_pass_keeps_its_name() -> None:
    assert _Collaborator.nested_pass.__name__ == "nested_pass"
