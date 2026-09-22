"""MCP wiring tests for extract/inline edits."""

from __future__ import annotations

import asyncio
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.extract_inline_helpers import (
    PROJECT,
    _extract_inline_repo,  # noqa: F401 - pytest fixture
    _qn,
)
from evals.cgr_graph import _StatefulIngestor


def test_mcp_extract_and_inline_tools(
    extract_inline_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, updater = extract_inline_repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    names = {s.name for s in registry.get_tool_schemas()}
    assert {cs.MCPToolName.EXTRACT, cs.MCPToolName.INLINE} <= names

    async def run() -> tuple[object, object]:
        refused = await registry.extract(
            qualified_name=_qn("pkg.report.build"),
            start_line=12,
            end_line=15,
            new_name="tail",
            project=PROJECT,
        )
        payload = await registry.inline(
            qualified_name=_qn("pkg.util.wrapper"), project=PROJECT
        )
        return refused, payload

    refused, payload = asyncio.run(run())
    assert isinstance(refused, dict) and cs.DICT_KEY_ERROR in refused
    assert isinstance(payload, dict) and payload["applied"] is True
    assert payload[cs.KEY_VERDICT]["ok"] is True
