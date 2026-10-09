"""The conditional purge contract (`cgr prune`, issue #2479).

`delete_project(expected_root=...)` must route to the conditional query with
the root bound under its own key: a param-name typo or a branch removed back
to the unconditional delete would pass the CLI mocks (whose return value is
theirs) and only the live repro would notice.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.services.graph_service import MemgraphIngestor


@pytest.fixture
def calls() -> dict[str, object]:
    return {"fetch": None, "execute": [], "pruned_resources": False}


@pytest.fixture
def ingestor(calls: dict[str, object]) -> MemgraphIngestor:
    def _fetch(self, query: str, params: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
        calls["fetch"] = (query, params)
        return calls.get("rows", [])

    def _execute(self, query: str, params: dict[str, object] | None = None) -> None:  # type: ignore[no-untyped-def]
        calls["execute"].append((query, params))

    def _prune_resources(_ingestor: MemgraphIngestor) -> None:
        calls["pruned_resources"] = True

    with (
        patch.object(MemgraphIngestor, "fetch_all", autospec=True, side_effect=_fetch),
        patch.object(
            MemgraphIngestor, "_execute_query", autospec=True, side_effect=_execute
        ),
        patch(
            "codebase_rag.services.graph_service.prune_unanchored_resources",
            side_effect=_prune_resources,
        ),
    ):
        with patch.object(MemgraphIngestor, "__init__", lambda self: None):
            yield MemgraphIngestor()


def test_conditional_root_delete_fires_when_the_root_matches(
    ingestor: MemgraphIngestor, calls: dict[str, object]
) -> None:
    calls["rows"] = [{cs.KEY_DELETED_COUNT: 1}]
    fired = ingestor.delete_project("proj__1", expected_root="/repo")

    assert fired is True
    query, params = calls["fetch"]  # type: ignore[misc]
    assert query == cq.CYPHER_DELETE_PROJECT_IF_ROOT
    assert params == {cs.KEY_PROJECT_NAME: "proj__1", cs.KEY_EXPECTED_ROOT: "/repo"}
    assert calls["pruned_resources"] is True
    assert calls["execute"] != []


def test_conditional_root_delete_does_not_fire_on_a_mismatch(
    ingestor: MemgraphIngestor, calls: dict[str, object]
) -> None:
    calls["rows"] = [{cs.KEY_DELETED_COUNT: 0}]
    fired = ingestor.delete_project("proj__1", expected_root="/repo")

    assert fired is False
    assert calls["pruned_resources"] is False
    assert calls["execute"] == []


def test_conditional_root_delete_fails_closed_on_an_unprovable_read(
    ingestor: MemgraphIngestor, calls: dict[str, object]
) -> None:
    calls["rows"] = []
    fired = ingestor.delete_project("proj__1", expected_root="/repo")

    assert fired is False
    assert calls["pruned_resources"] is False


def test_unconditional_delete_ignores_the_root(
    ingestor: MemgraphIngestor, calls: dict[str, object]
) -> None:
    fired = ingestor.delete_project("proj__1")

    assert fired is True
    assert calls["fetch"] is None
    execute = calls["execute"]  # type: ignore[misc]
    assert execute[0] == (cq.CYPHER_DELETE_PROJECT, {cs.KEY_PROJECT_NAME: "proj__1"})
    assert calls["pruned_resources"] is True
