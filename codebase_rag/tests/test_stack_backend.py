from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.stack import constants as sc
from codebase_rag.stack.health import wait_for_graph


def test_compose_defines_both_engines_under_profiles() -> None:
    compose = (Path("codebase_rag") / "docker-compose.yaml").read_text()
    assert "arcadedata/arcadedb" in compose
    assert "BoltProtocolPlugin" in compose
    # Both default to 7687, so neither may start unprofiled.
    assert compose.count("profiles:") >= 2


def test_service_names_cover_both_backends() -> None:
    assert sc.SERVICE_MEMGRAPH == "memgraph"
    assert sc.SERVICE_ARCADEDB == "arcadedb"


def test_wait_for_graph_probes_bolt_for_memgraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "codebase_rag.stack.health._bolt_reachable",
        lambda h, p: seen.append((h, p)) or True,
    )
    assert wait_for_graph(GraphBackend.MEMGRAPH, "h", 7687, None, timeout=1.0)
    assert seen == [("h", 7687)]


def test_wait_for_graph_requires_both_ports_for_arcadedb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live Bolt listener with a dead HTTP endpoint would pass startup and
    # then fail at ensure_constraints(), so both must be probed.
    monkeypatch.setattr(
        "codebase_rag.stack.health._arcade_bolt_reachable", lambda h, p: True
    )
    monkeypatch.setattr("codebase_rag.stack.health._http_reachable", lambda url: False)
    assert not wait_for_graph(GraphBackend.ARCADEDB, "h", 7687, 2480, timeout=1.0)
