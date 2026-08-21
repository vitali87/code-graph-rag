# The crash-correlation MCP tools (issue #227): registration, JSON-shaped
# results, and project scoping through the server's repository root.

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.crash_correlation import CYPHER_CRASH_CALLS
from codebase_rag.cypher_queries import CYPHER_TRACE_CALLABLES
from codebase_rag.flow_verdict import CYPHER_FLOW_COVERAGE_GAPS, CYPHER_FLOW_EDGES
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.utils.path_utils import derive_project_name

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _registry(tmp_path: Path) -> MCPToolsRegistry:
    project = derive_project_name(tmp_path)
    module = f"{project}.app.service"

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        if query == CYPHER_TRACE_CALLABLES:
            return [
                {
                    cs.KEY_LABEL: cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME: f"{module}.handle",
                    cs.KEY_PATH: "app/service.py",
                    cs.KEY_START_LINE: 8,
                    cs.KEY_END_LINE: 12,
                },
                {
                    cs.KEY_LABEL: cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME: f"{module}.dispatch",
                    cs.KEY_PATH: "app/service.py",
                    cs.KEY_START_LINE: 14,
                    cs.KEY_END_LINE: 18,
                },
            ]
        if query == CYPHER_CRASH_CALLS:
            return [{"from_qn": f"{module}.dispatch", "to_qn": f"{module}.handle"}]
        if query in (CYPHER_FLOW_EDGES, CYPHER_FLOW_COVERAGE_GAPS):
            return []
        raise AssertionError(f"unexpected query: {query}")

    ingestor = MagicMock()
    ingestor.fetch_all = fetch_all
    return MCPToolsRegistry(
        project_root=str(tmp_path),
        ingestor=ingestor,
        cypher_gen=MagicMock(),
    )


def _crash_text(tmp_path: Path) -> str:
    src = (tmp_path / "app" / "service.py").as_posix()
    return (
        "Traceback (most recent call last):\n"
        f'  File "{src}", line 16, in dispatch\n'
        "    return handle(cfg)\n"
        f'  File "{src}", line 10, in handle\n'
        "    return cfg.timeout\n"
        "AttributeError: 'NoneType' object has no attribute 'timeout'\n"
    )


async def test_tools_are_registered_with_the_traceback_parameter(tmp_path):
    registry = _registry(tmp_path)
    for name in (cs.MCPToolName.EXPLAIN_TRACEBACK, cs.MCPToolName.RANK_ROOT_CAUSES):
        tool = registry._tools[name]
        assert cs.MCPParamName.TRACEBACK_TEXT in tool.input_schema["properties"]
        assert tool.input_schema["required"] == [cs.MCPParamName.TRACEBACK_TEXT]
        assert tool.returns_json is True


async def test_explain_traceback_returns_resolved_frames(tmp_path):
    registry = _registry(tmp_path)
    project = derive_project_name(tmp_path)
    result = await registry.explain_traceback(traceback_text=_crash_text(tmp_path))
    assert result["exception_type"] == "AttributeError"
    qns = [frame["qualified_name"] for frame in result["frames"]]
    assert qns == [f"{project}.app.service.dispatch", f"{project}.app.service.handle"]


async def test_rank_root_causes_returns_ranked_candidates(tmp_path):
    registry = _registry(tmp_path)
    project = derive_project_name(tmp_path)
    result = await registry.rank_root_causes(traceback_text=_crash_text(tmp_path))
    assert result["failing"] == f"{project}.app.service.handle"
    assert result["flow_used"] is False
    top = result["candidates"][0]
    assert top["qualified_name"] == f"{project}.app.service.dispatch"
    assert top["call_path"] == (
        f"{project}.app.service.dispatch",
        f"{project}.app.service.handle",
    )
