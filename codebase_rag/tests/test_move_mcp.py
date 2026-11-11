# The MCP `move` tool (issue #1534): driven through the handler the registry
# serves, and held to the root guard `rename` and `change_signature` apply,
# so a project indexed from another checkout never has its repo-relative
# paths edited beneath this server's repository.

from __future__ import annotations

import re
import shutil
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.readme_sections import format_mcp_tools_table
from codebase_rag.tests.test_move_op import FIXTURE, PROJECT, _index, _materialise
from codebase_rag.tools import tool_descriptions as td
from evals.cgr_graph import _StatefulIngestor

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "guide" / "mcp-server.md"


def _qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _registry(
    root: Path, store: _StatefulIngestor, updater: GraphUpdater
) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    return registry


async def test_the_served_move_handler_refuses_and_applies_as_json(
    temp_repo: Path,
) -> None:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    entry = _registry(root, store, updater).get_tool_handler(cs.MCPToolName.MOVE)
    assert entry is not None
    handler, returns_json = entry
    assert returns_json is True

    refused = await handler(
        qualified_name=_qn("pkg.util.helper"), target_module="pkg.util", project=PROJECT
    )
    assert isinstance(refused, dict)
    assert "already holds" in refused[cs.DICT_KEY_ERROR]
    assert refused[cs.KEY_CYCLE] == []

    payload = await handler(
        qualified_name=_qn("pkg.util.helper"), target_module="pkg.core", project=PROJECT
    )
    assert isinstance(payload, dict)
    assert payload["applied"] is True, payload
    assert payload["new_qualified_name"] == _qn("pkg.core.helper")
    assert payload[cs.KEY_VERDICT]["ok"] is True
    assert (root / "pkg/core.py").exists()


async def test_move_refuses_a_project_indexed_from_another_checkout(
    temp_repo: Path,
) -> None:
    # The graph project was indexed from `indexed`; this server is rooted at
    # a sibling checkout holding the same repo-relative paths. Editing there
    # would cut and rewrite files the graph never described.
    indexed = _materialise(temp_repo, FIXTURE)
    store, updater = _index(indexed)
    served = temp_repo / "other_checkout"
    shutil.copytree(indexed, served)
    before = {rel: (served / rel).read_text() for rel in FIXTURE}

    entry = _registry(served, store, updater).get_tool_handler(cs.MCPToolName.MOVE)
    assert entry is not None
    payload = await entry[0](
        qualified_name=_qn("pkg.util.helper"), target_module="pkg.core", project=PROJECT
    )

    assert isinstance(payload, dict)
    assert payload[cs.DICT_KEY_ERROR] == cs.MOVE_WRONG_ROOT.format(project=PROJECT)
    assert payload[cs.KEY_CYCLE] == []
    for rel, text in before.items():
        assert (served / rel).read_text() == text
    assert not (served / "pkg/core.py").exists()
    assert not (indexed / "pkg/core.py").exists()


def test_move_dry_run_parameter_does_not_promise_a_diff() -> None:
    # A move plan reports files, importers and copied imports; it stages no
    # diff, so the schema must not advertise one.
    registry = MCPToolsRegistry(
        project_root=".", ingestor=MagicMock(), cypher_gen=MagicMock()
    )
    schema = next(
        s for s in registry.get_tool_schemas() if s.name == cs.MCPToolName.MOVE
    )
    described = schema.inputSchema["properties"][cs.MCPParamName.DRY_RUN]
    assert described["description"] == td.MCP_PARAM_MOVE_DRY_RUN
    assert "diff" not in described["description"]


def test_mcp_guide_tools_table_is_the_generated_one() -> None:
    # A stale copy of a row survived a regeneration once (`query_code_graph`
    # listed twice with contradicting contracts); the checked-in section
    # must be exactly what the generator renders.
    text = _DOCS.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- SECTION:mcp_tools -->\n(.*?)\n<!-- /SECTION:mcp_tools -->",
        text,
        re.DOTALL,
    )
    assert match is not None
    assert match.group(1) == format_mcp_tools_table()
    names = re.findall(r"^\| `(\w+)` \|", match.group(1), re.MULTILINE)
    assert len(names) == len(set(names))
