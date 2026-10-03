"""Endpoint tools say when the project was synced without `io` (issue #2521).

`io` is an opt-in capture group, so a project synced with the default set
has no EXPOSES, READS_FROM or WRITES_TO edges at all, and `endpoints`,
`endpoint_callers` and `remote_dependencies` answered `[]`: the same answer
as a service that genuinely exposes nothing. The sync now records the
relationships it captured on the Project node, and an empty answer from a
project that did not capture what the tool reads names the missing group
and how to re-index instead.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow

P = "orders"
ROOT = "/srv/orders"
_CAPTURED_KEY = "captured_relationships"
_DEFAULT = sorted(rel.value for rel in resolve_capture([]).enabled_rels)
_WITH_IO = sorted(
    rel.value for rel in resolve_capture([cs.CaptureGroup.IO.value]).enabled_rels
)
_ROW = {
    "endpoint": "GET /orders/{id}",
    "kind": "ENDPOINT",
    "label": "Function",
    "handler": f"{P}.api.get_order",
    "path": "api.py",
    "callers": 0,
}


class _Graph:
    """One project; answers the Project read and the endpoint reads."""

    def __init__(
        self,
        captured: list[str] | None,
        rows: list[ResultRow] | None = None,
        callers: list[ResultRow] | None = None,
    ) -> None:
        self.captured = captured
        self.rows = rows or []
        self.callers = callers or []
        self.queries: list[str] = []

    def list_projects(self) -> list[str]:
        return [P]

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        self.queries.append(query)
        if _CAPTURED_KEY in query:
            return [{_CAPTURED_KEY: self.captured, cs.KEY_ROOT_PATH: ROOT}]
        if query == cq.CYPHER_GRAPH_ENDPOINTS:
            return list(self.rows)
        if query == cq.CYPHER_GRAPH_ENDPOINT_CALLERS:
            return list(self.callers)
        return []


def _registry(tmp_path: Path, graph: _Graph) -> MCPToolsRegistry:
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=graph, cypher_gen=MagicMock()
        )


async def _ask(registry: MCPToolsRegistry, tool: cs.MCPToolName) -> object:
    if tool is cs.MCPToolName.ENDPOINTS:
        return await registry.endpoints(project=P)
    if tool is cs.MCPToolName.ENDPOINT_CALLERS:
        return await registry.endpoint_callers("GET /orders/{id}", project=P)
    return await registry.remote_dependencies(project=P)


_ENDPOINT_TOOLS = (
    cs.MCPToolName.ENDPOINTS,
    cs.MCPToolName.ENDPOINT_CALLERS,
    cs.MCPToolName.REMOTE_DEPENDENCIES,
)


class TestSyncRecordsTheCapture:
    def _project_props(self, tmp_path: Path, tokens: list[str]) -> dict:
        (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        parsers, queries = load_parsers()
        mock = MagicMock()
        GraphUpdater(
            ingestor=mock,
            repo_path=tmp_path,
            parsers=parsers,
            queries=queries,
            capture=resolve_capture(tokens),
        ).run()
        return next(
            call.args[1]
            for call in mock.ensure_node_batch.call_args_list
            if call.args[0] == cs.NodeLabel.PROJECT
        )

    def test_a_default_sync_records_a_capture_without_io(self, tmp_path: Path) -> None:
        props = self._project_props(tmp_path, [])
        assert props[_CAPTURED_KEY] == _DEFAULT
        assert cs.RelationshipType.EXPOSES.value not in props[_CAPTURED_KEY]

    def test_an_io_sync_records_the_io_relationships(self, tmp_path: Path) -> None:
        props = self._project_props(tmp_path, [cs.CaptureGroup.IO.value])
        assert props[_CAPTURED_KEY] == _WITH_IO
        for rel in cs.CAPTURE_GROUP_RELS[cs.CaptureGroup.IO]:
            assert rel.value in props[_CAPTURED_KEY]


class TestUncapturedIoIsSaid:
    @pytest.mark.anyio
    @pytest.mark.parametrize("tool", _ENDPOINT_TOOLS)
    async def test_an_empty_answer_names_the_missing_group(
        self, tmp_path: Path, tool: cs.MCPToolName
    ) -> None:
        answer = await _ask(_registry(tmp_path, _Graph(_DEFAULT)), tool)
        assert isinstance(answer, dict), answer
        message = answer[cs.DICT_KEY_ERROR]
        assert "`io`" in message, message
        assert f"cgr start --repo-path {ROOT} --update-graph --capture io" in message, (
            message
        )


def _io_without(*left_out: cs.RelationshipType) -> list[str]:
    return [rel for rel in _WITH_IO if rel not in {r.value for r in left_out}]


_CALLER = {
    "label": "Function",
    "qualified_name": "web.client.load_order",
    "path": "client.py",
    "url": "/orders/{id}",
    "direction": cs.RelationshipType.READS_FROM.value,
    "endpoint": "GET /orders/{id}",
    "handler": f"{P}.api.get_order",
}


class TestCallersNeedTheCallSideCaptured:
    # Bot review on PR #2596: `endpoint_callers` reads the handler's EXPOSES
    # and the callers' READS_FROM / WRITES_TO, joined through RESOLVES_TO for
    # a URL, but only EXPOSES was checked. A project that captured EXPOSES
    # alone answered `[]`: "no callers", where none could have been recorded.
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "left_out",
        [
            (cs.RelationshipType.READS_FROM, cs.RelationshipType.WRITES_TO),
            (cs.RelationshipType.WRITES_TO,),
            (cs.RelationshipType.RESOLVES_TO,),
        ],
        ids=["reads-and-writes", "writes", "resolves-to"],
    )
    async def test_an_empty_answer_names_the_relationships_left_out(
        self, tmp_path: Path, left_out: tuple[cs.RelationshipType, ...]
    ) -> None:
        graph = _Graph(_io_without(*left_out))
        answer = await _ask(_registry(tmp_path, graph), cs.MCPToolName.ENDPOINT_CALLERS)
        assert isinstance(answer, dict), answer
        message = answer[cs.DICT_KEY_ERROR]
        assert "`io`" in message, message
        assert ", ".join(sorted(rel.value for rel in left_out)) in message, message
        assert f"cgr start --repo-path {ROOT} --update-graph --capture io" in message, (
            message
        )

    @pytest.mark.anyio
    async def test_callers_found_are_returned_whatever_was_left_out(
        self, tmp_path: Path
    ) -> None:
        graph = _Graph(
            _io_without(cs.RelationshipType.READS_FROM, cs.RelationshipType.WRITES_TO),
            callers=[_CALLER],
        )
        answer = await _ask(_registry(tmp_path, graph), cs.MCPToolName.ENDPOINT_CALLERS)
        assert answer == [_CALLER]
        assert not any(_CAPTURED_KEY in query for query in graph.queries)

    @pytest.mark.anyio
    async def test_callers_found_with_everything_captured_are_unchanged(
        self, tmp_path: Path
    ) -> None:
        graph = _Graph(_WITH_IO, callers=[_CALLER])
        answer = await _ask(_registry(tmp_path, graph), cs.MCPToolName.ENDPOINT_CALLERS)
        assert answer == [_CALLER]

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("tool", "left_out"),
        [
            (
                cs.MCPToolName.ENDPOINTS,
                (cs.RelationshipType.READS_FROM, cs.RelationshipType.WRITES_TO),
            ),
            (
                cs.MCPToolName.REMOTE_DEPENDENCIES,
                (cs.RelationshipType.EXPOSES, cs.RelationshipType.RESOLVES_TO),
            ),
        ],
    )
    async def test_the_other_tools_still_need_only_what_they_read(
        self,
        tmp_path: Path,
        tool: cs.MCPToolName,
        left_out: tuple[cs.RelationshipType, ...],
    ) -> None:
        # Their emptiness turns on what they MATCH, not on what they count or
        # optionally join: `endpoints` on EXPOSES, `remote_dependencies` on
        # READS_FROM / WRITES_TO.
        graph = _Graph(_io_without(*left_out))
        assert await _ask(_registry(tmp_path, graph), tool) == []


class TestWhatStaysAnAnswer:
    @pytest.mark.anyio
    @pytest.mark.parametrize("tool", _ENDPOINT_TOOLS)
    async def test_an_io_project_with_nothing_to_report_is_still_empty(
        self, tmp_path: Path, tool: cs.MCPToolName
    ) -> None:
        assert await _ask(_registry(tmp_path, _Graph(_WITH_IO)), tool) == []

    @pytest.mark.anyio
    @pytest.mark.parametrize("tool", _ENDPOINT_TOOLS)
    async def test_a_graph_synced_before_the_record_is_not_flagged(
        self, tmp_path: Path, tool: cs.MCPToolName
    ) -> None:
        # No recorded capture is not evidence that io was off.
        assert await _ask(_registry(tmp_path, _Graph(None)), tool) == []

    @pytest.mark.anyio
    async def test_rows_are_returned_without_reading_the_capture(
        self, tmp_path: Path
    ) -> None:
        graph = _Graph(_DEFAULT, rows=[_ROW])
        answer = await _registry(tmp_path, graph).endpoints(project=P)
        assert answer == [_ROW]
        assert not any(_CAPTURED_KEY in query for query in graph.queries)

    @pytest.mark.anyio
    async def test_other_graph_tools_are_unaffected(self, tmp_path: Path) -> None:
        # `resolve` reads definitions, which every capture set records; its
        # empty answer means what it always meant.
        graph = _Graph(_DEFAULT)
        registry = _registry(tmp_path, graph)
        assert await registry.resolve("get_order", project=P) == []
        assert not any(_CAPTURED_KEY in query for query in graph.queries)
