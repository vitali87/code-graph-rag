from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from move_helpers import (  # noqa: F401
    PROJECT,
    _move_repo,  # noqa: F401
    qn,
)

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from evals.cgr_graph import _StatefulIngestor


def test_mcp_move_tool_reports_and_refuses(
    move_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = move_repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    schema = next(
        s for s in registry.get_tool_schemas() if s.name == cs.MCPToolName.MOVE
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.TARGET_MODULE,
    }
    refused = asyncio.run(
        registry.move(
            qualified_name=qn("pkg.util.helper"),
            target_module="pkg.util",
            project=PROJECT,
        )
    )
    assert isinstance(refused, dict) and cs.DICT_KEY_ERROR in refused
    payload = asyncio.run(
        registry.move(
            qualified_name=qn("pkg.util.helper"),
            target_module="pkg.core",
            project=PROJECT,
        )
    )
    assert isinstance(payload, dict)
    assert payload["applied"] is True
    assert payload["new_qualified_name"] == qn("pkg.core.helper")
    assert payload[cs.KEY_VERDICT]["ok"] is True
