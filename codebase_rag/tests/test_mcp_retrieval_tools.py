"""`RetrievalTools`, the MCP handlers that answer through the agent's own
tools, driven on the mixin itself with the real query, duplicate, source
and snippet tools over a scripted graph. Pinned directly: each handler's
partial-graph refusal, checked before the tool runs; the unknown-project
refusal before any model is asked; a per-request query tool bound to the
project named; the wire shape of a query answer and of a refusal; a missing
snippet worded as an unknown name; and `ask_agent` denying every approval
it would need an operator for."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from rich.console import Console

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import exceptions as ex
from codebase_rag import tool_errors as te
from codebase_rag.mcp import constants as mcs
from codebase_rag.mcp.retrieval_tools import RetrievalTools
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.tests.conftest import marker_store
from codebase_rag.tools.code_retrieval import CodeRetriever, create_code_retrieval_tool
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.tools.duplicate_detection import create_find_duplicates_tool
from codebase_rag.tools.semantic_search import create_get_function_source_tool
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo

pytestmark = [pytest.mark.anyio]

OTHER = "other__0000aaaa"
QN_CYPHER = "MATCH (n) RETURN n.qualified_name AS qualified_name"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class _Retrieval:
    """A `RetrievalTools` over a graph answering `rows` to the generated
    query and `answers` by query text, with markers kept by `marker_store`."""

    def __init__(self, root: Path, rows: list[ResultRow] | None = None) -> None:
        self.root = root
        self.project = derive_project_name(root)
        self.answers: dict[str, list[ResultRow]] = {}
        self.store = marker_store()
        self.store.list_projects.return_value = [self.project, OTHER]
        read_marker = self.store.fetch_all.side_effect

        def fetch_all(query: str, params: PropertyDict | None = None) -> list:
            if "run_incomplete" in query:
                return read_marker(query, params)
            return list(self.answers.get(query, []))

        self.store.fetch_all.side_effect = fetch_all
        self.store.fetch_read_only.side_effect = lambda _q: list(rows or [])
        self.cypher_gen = MagicMock()
        self.cypher_gen.generate = AsyncMock(return_value=QN_CYPHER)

        tools = RetrievalTools()
        tools.project_root = str(root)
        tools.ingestor = self.store
        tools.cypher_gen = self.cypher_gen
        tools.workspace = None
        tools._ingestor_lock = asyncio.Lock()
        tools._live_updater = None
        tools.marker = RunMarker(lambda: tools.ingestor)
        tools._stderr_console = Console(file=io.StringIO())
        tools._query_tool = create_query_tool(
            ingestor=self.store,
            cypher_gen=self.cypher_gen,
            console=tools._stderr_console,
        )
        tools._code_tool = create_code_retrieval_tool(
            code_retriever=CodeRetriever(str(root), self.store)
        )
        tools._find_duplicates_tool = create_find_duplicates_tool(self.store)
        tools._function_source_tool = create_get_function_source_tool(self.store)
        tools._semantic_search_tool = None
        tools._rag_agent = None
        self.tools = tools

    def damage(self, project: str) -> None:
        self.tools.marker.invalidate(project)

    def refusal(self, tool: cs.MCPToolName, project: str | None = None) -> str:
        return mcs.MCP_QUERY_AFTER_FAILED_RUN.format(
            project=project or self.project, tool=tool
        )


@pytest.fixture
def retrieval(tmp_path: Path) -> _Retrieval:
    return _Retrieval(tmp_path)


# --- query_code_graph -----------------------------------------------------------


async def test_a_query_answer_carries_no_error_key(tmp_path: Path) -> None:
    rows = [{"qualified_name": "a.b"}]
    retrieval = _Retrieval(tmp_path, rows)

    result = await retrieval.tools.query_code_graph("functions")

    # An `error` key marks a refusal: a successful answer must not carry one,
    # not even as None.
    assert mcs.MCP_KEY_ERROR not in result
    assert result["query_used"] == QN_CYPHER
    assert result["results"] == rows


async def test_a_scoped_query_is_answered_by_a_tool_bound_to_its_project(
    tmp_path: Path,
) -> None:
    project = derive_project_name(tmp_path)
    rows = [
        {"qualified_name": f"{OTHER}.mod.f"},
        {"qualified_name": f"{project}.mod.g"},
    ]
    retrieval = _Retrieval(tmp_path, rows)

    result = await retrieval.tools.query_code_graph("functions", project=project)

    assert result["results"] == [{"qualified_name": f"{project}.mod.g"}]


async def test_an_unknown_project_is_refused_before_any_model_is_asked(
    retrieval: _Retrieval,
) -> None:
    result = await retrieval.tools.query_code_graph("functions", project="nope")

    assert "nope" in result["error"]
    assert result["query_used"] == cs.QUERY_NOT_AVAILABLE
    assert result["results"] == []
    retrieval.cypher_gen.generate.assert_not_awaited()


async def test_a_query_over_a_partial_graph_is_refused(
    retrieval: _Retrieval,
) -> None:
    retrieval.damage(retrieval.project)

    result = await retrieval.tools.query_code_graph("functions")

    assert result == {
        "error": retrieval.refusal(cs.MCPToolName.QUERY_CODE_GRAPH),
        "query_used": cs.QUERY_NOT_AVAILABLE,
        "results": [],
    }
    retrieval.cypher_gen.generate.assert_not_awaited()


async def test_a_query_whose_project_list_fails_is_a_refusal(
    retrieval: _Retrieval,
) -> None:
    retrieval.store.list_projects.side_effect = RuntimeError("store down")

    result = await retrieval.tools.query_code_graph("x", project=OTHER)

    assert result["error"] == mcs.MCP_TOOL_EXEC_ERROR.format(
        name=cs.MCPToolName.QUERY_CODE_GRAPH, error="store down"
    )


# --- semantic_search ------------------------------------------------------------


def _search_tool(calls: list[dict]) -> Tool:
    async def semantic_search(
        query: str, top_k: int = 5, project: str | None = None
    ) -> str:
        calls.append({"query": query, "top_k": top_k, "project": project})
        return "hits"

    return Tool(semantic_search)


async def test_semantic_search_without_its_extra_says_what_to_install(
    retrieval: _Retrieval,
) -> None:
    result = await retrieval.tools.semantic_search("auth")

    assert result == te.failure(ex.SEMANTIC_EXTRA)
    retrieval.store.list_projects.assert_not_called()


async def test_semantic_search_forwards_its_scope_to_the_vector_search(
    retrieval: _Retrieval,
) -> None:
    calls: list[dict] = []
    retrieval.tools._semantic_search_tool = _search_tool(calls)

    assert await retrieval.tools.semantic_search("auth", 3, project=OTHER) == "hits"
    assert calls == [{"query": "auth", "top_k": 3, "project": OTHER}]


async def test_semantic_search_refuses_an_unknown_or_partial_project(
    retrieval: _Retrieval,
) -> None:
    calls: list[dict] = []
    retrieval.tools._semantic_search_tool = _search_tool(calls)
    retrieval.damage(retrieval.project)

    unknown = await retrieval.tools.semantic_search("auth", project="nope")
    # Unscoped, the search hydrates hits for the project this root derives to.
    partial = await retrieval.tools.semantic_search("auth")

    assert isinstance(unknown, te.ToolFailure)
    assert "nope" in unknown
    assert partial == retrieval.refusal(cs.MCPToolName.SEMANTIC_SEARCH)
    assert calls == []


# --- find_duplicate_code, get_function_source ------------------------------------


@pytest.mark.parametrize(
    ("tool", "call"),
    [
        (
            cs.MCPToolName.FIND_DUPLICATE_CODE,
            lambda tools: tools.find_duplicate_code(),
        ),
        (
            cs.MCPToolName.GET_FUNCTION_SOURCE,
            lambda tools: tools.get_function_source(7),
        ),
    ],
)
async def test_a_graph_reader_refuses_a_partial_graph(
    retrieval: _Retrieval,
    tool: cs.MCPToolName,
    call: Callable[[RetrievalTools], object],
) -> None:
    complete = await call(retrieval.tools)
    retrieval.damage(retrieval.project)

    refused = await call(retrieval.tools)

    assert complete != refused
    assert refused == retrieval.refusal(tool)


async def test_find_duplicate_code_refuses_another_projects_damage_only(
    retrieval: _Retrieval,
) -> None:
    retrieval.damage(OTHER)

    assert await retrieval.tools.find_duplicate_code(project=OTHER) == (
        retrieval.refusal(cs.MCPToolName.FIND_DUPLICATE_CODE, OTHER)
    )
    assert await retrieval.tools.find_duplicate_code() != retrieval.refusal(
        cs.MCPToolName.FIND_DUPLICATE_CODE
    )


# --- get_code_snippet -----------------------------------------------------------


async def test_a_snippet_is_read_from_the_recorded_lines(
    retrieval: _Retrieval,
) -> None:
    (retrieval.root / "mod.py").write_text(
        "import os\n\n\ndef f():\n    return 1\n", encoding="utf-8"
    )
    qn = f"{retrieval.project}.mod.f"
    retrieval.answers[cq.CYPHER_FIND_BY_QUALIFIED_NAME] = [
        {"path": "mod.py", "start": 4, "end": 5, "docstring": None}
    ]

    result = await retrieval.tools.get_code_snippet(qn)

    assert result["found"] is True
    assert result["source_code"] == "def f():\n    return 1\n"


async def test_a_missing_snippet_is_worded_as_an_unknown_name(
    retrieval: _Retrieval,
) -> None:
    qn = f"{retrieval.project}.mod.gone"

    result = await retrieval.tools.get_code_snippet(qn)

    assert result["found"] is False
    assert result["error"] == (
        mcs.MCP_UNKNOWN_TARGET.format(qualified_name=qn) + mcs.MCP_UNKNOWN_TARGET_HINT
    )


async def test_a_snippet_from_a_partial_graph_is_refused(
    retrieval: _Retrieval,
) -> None:
    retrieval.damage(retrieval.project)

    result = await retrieval.tools.get_code_snippet(f"{retrieval.project}.mod.f")

    assert result == {
        "error": retrieval.refusal(cs.MCPToolName.GET_CODE_SNIPPET),
        "found": False,
    }


# --- ask_agent ------------------------------------------------------------------


def _agent_editing(path: str, edited: list[str]) -> Agent:
    # A model that asks for an edit needing approval, then answers with
    # whatever the edit returned.
    def edit(file_path: str) -> str:
        edited.append(file_path)
        return "edited"

    def model(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        returned = [
            part
            for message in messages
            for part in getattr(message, "parts", [])
            if isinstance(part, ToolReturnPart)
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("edit", {"file_path": path})])
        return ModelResponse(parts=[TextPart(f"edit said: {returned[-1].content}")])

    return Agent(
        FunctionModel(model),
        output_type=[str, DeferredToolRequests],
        tools=[Tool(edit, requires_approval=True)],
    )


async def test_ask_agent_denies_what_would_need_an_operator(
    retrieval: _Retrieval,
) -> None:
    edited: list[str] = []
    retrieval.tools._rag_agent = _agent_editing("a.py", edited)

    result = await retrieval.tools.ask_agent("rename a")

    assert result == {"output": f"edit said: {mcs.MCP_ASK_AGENT_APPROVAL_DENIED}"}
    assert edited == []


async def test_ask_agent_refuses_a_partial_graph_without_running(
    retrieval: _Retrieval,
) -> None:
    edited: list[str] = []
    retrieval.tools._rag_agent = _agent_editing("a.py", edited)
    retrieval.damage(retrieval.project)

    result = await retrieval.tools.ask_agent("rename a")

    assert result == {"error": retrieval.refusal(cs.MCPToolName.ASK_AGENT)}


async def test_an_agent_failure_is_an_error_answer(retrieval: _Retrieval) -> None:
    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model down")

    retrieval.tools._rag_agent = Agent(
        FunctionModel(model), output_type=[str, DeferredToolRequests]
    )

    result = await retrieval.tools.ask_agent("anything")

    assert result == {"error": mcs.MCP_ASK_AGENT_ERROR.format(error="model down")}


# --- the agent's query tool -----------------------------------------------------


def test_without_a_workspace_the_agent_gets_the_prebuilt_query_tool(
    retrieval: _Retrieval,
) -> None:
    assert retrieval.tools._agent_query_tool() is retrieval.tools._query_tool


async def test_a_workspace_default_not_indexed_gets_a_refusing_query_tool(
    retrieval: _Retrieval, tmp_path: Path
) -> None:
    # The workspace serves this root under a name the graph does not hold.
    retrieval.tools.workspace = WorkspaceConfig(
        name="ws", repos=[WorkspaceRepo(path=str(tmp_path), project_name="served")]
    )

    tool = retrieval.tools._agent_query_tool()
    answer = await asyncio.to_thread(tool.function, "functions")

    assert tool is not retrieval.tools._query_tool
    assert "served" in answer
    retrieval.cypher_gen.generate.assert_not_awaited()
