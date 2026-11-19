"""PR #2900 review: a cold MCP rename takes off the marker its hydration put
down only when nothing it did changed the graph or the tree.

A rename with no retained updater hydrates one, which marks the project and
runs the constraint migration. When the rename then never re-ingests (a
preview, a refusal), the marker came off unless a probe, read before the
migration, foresaw a purge. With a legacy constraint in force the migration
purges every Folder/File two projects own, which no probe sees for projects
over one root, so the graph lost nodes while the marker came off. And a
rename that committed its edits but failed to read the graph before
re-ingesting had its marker taken off before it was put back.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tests.test_rename_op import (  # noqa: F401 - fixture
    RecordedGraph,
    py_repo,
)


def _registry(
    root: Path,
    graph: RecordedGraph,
    monkeypatch: pytest.MonkeyPatch,
    purged: int | None,
    reported: bool = True,
) -> tuple[MCPToolsRegistry, list[tuple[str, bool]]]:
    ingestor = MagicMock()
    ingestor.fetch_all = graph.fetch_all
    ingestor.list_projects.return_value = [graph.project]
    if reported:
        ingestor.ensure_constraints.return_value = purged
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    writes: list[tuple[str, bool]] = []

    def persist(project: str, incomplete: bool, *, writing: bool = True) -> bool:
        writes.append((project, incomplete))
        return True

    monkeypatch.setattr(registry, "_persist_incomplete", persist)
    return registry, writes


async def _preview(
    registry: MCPToolsRegistry, graph: RecordedGraph
) -> dict[str, str | bool | list[str] | None]:
    return await registry.rename(
        qualified_name=f"{graph.project}.pkg.util.helper",
        new_name="assist",
        dry_run=True,
        project=graph.project,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purged", "reported"),
    [(3, True), (None, True), (None, False)],
    ids=["purged", "none", "not-reported"],
)
async def test_a_preview_keeps_the_marker_unless_hydration_purged_nothing(
    py_repo: tuple[Path, RecordedGraph],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    purged: int | None,
    reported: bool,
) -> None:
    root, graph = py_repo
    registry, writes = _registry(root, graph, monkeypatch, purged, reported)

    payload = await _preview(registry, graph)

    assert isinstance(payload, dict), payload
    assert payload["applied"] is False, payload
    assert (graph.project, True) in writes, writes
    assert (graph.project, False) not in writes, writes


@pytest.mark.asyncio
async def test_a_preview_whose_hydration_purged_nothing_clears_its_marker(
    py_repo: tuple[Path, RecordedGraph],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: issue #2801's fix stands when the migration removed nothing.
    root, graph = py_repo
    registry, writes = _registry(root, graph, monkeypatch, 0)

    await _preview(registry, graph)

    assert writes == [(graph.project, True), (graph.project, False)], writes


@pytest.mark.asyncio
async def test_a_committed_rename_that_never_reingested_keeps_its_marker(
    py_repo: tuple[Path, RecordedGraph],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The edits land, then the graph read before the re-ingest fails: the
    # marker must stay down throughout, not come off and go back on.
    root, graph = py_repo
    registry, writes = _registry(root, graph, monkeypatch, 0)

    with patch(
        "codebase_rag.editing.rename.measure",
        side_effect=ConnectionError("graph read failed"),
    ):
        payload = await registry.rename(
            qualified_name=f"{graph.project}.pkg.util.helper",
            new_name="assist",
            project=graph.project,
        )

    assert isinstance(payload, dict), payload
    assert payload["applied"] is True, payload
    assert payload["graph_incomplete"] is True, payload
    assert "def assist" in (root / "pkg" / "util.py").read_text()
    assert (graph.project, False) not in writes, writes
    assert writes, writes
    assert all(incomplete for _, incomplete in writes), writes
