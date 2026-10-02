"""Issue #2657: a non-interactive agent run ends with a text answer.

`cgr start -a` and the MCP `ask_agent` tool ran the agent once and printed
`response.output` whatever it was. When the model asked for a tool that
needs approval (`create_file`, `replace_code`, a non-read-only shell
command), the output was pydantic-ai's `DeferredToolRequests(...)` and its
repr became the answer, with exit 0 / `isError: false` and nothing done.
`--no-confirm` was never consulted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from codebase_rag import constants as cs
from codebase_rag.cli_runtime import app_context
from codebase_rag.main import main_single_query
from codebase_rag.mcp.tools import MCPToolsRegistry

QUESTION = "Add a NOTES.md file that says hello"
REPR = "DeferredToolRequests("


def _returns(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _agent(
    root: Path, *, insist: bool = False
) -> Agent[None, str | DeferredToolRequests]:
    def create_file(file_path: str, content: str) -> str:
        (root / file_path).write_text(content, encoding="utf-8")
        return f"wrote {file_path}"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _returns(messages)
        if returns and not insist:
            return ModelResponse(parts=[TextPart(f"Done: {returns[-1]}")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "create_file",
                    {"file_path": "NOTES.md", "content": "hello\n"},
                    tool_call_id=f"call_{len(returns)}",
                )
            ]
        )

    return Agent[None, str | DeferredToolRequests](
        FunctionModel(model),
        tools=[Tool(create_file, requires_approval=True)],
        output_type=[str, DeferredToolRequests],
    )


def _text_agent() -> Agent[None, str | DeferredToolRequests]:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("The parser builds a graph.")])

    return Agent[None, str | DeferredToolRequests](
        FunctionModel(model), output_type=[str, DeferredToolRequests]
    )


@pytest.fixture
def confirm_edits() -> Iterator[None]:
    before = app_context.session.confirm_edits
    yield
    app_context.session.confirm_edits = before


def _single_query(
    agent: Agent[None, str | DeferredToolRequests],
    output_format: cs.QueryFormat = cs.QueryFormat.TABLE,
) -> None:
    with (
        patch("codebase_rag.main._setup_common_initialization"),
        patch("codebase_rag.main.connect_memgraph") as connect,
        patch(
            "codebase_rag.main._initialize_services_and_agent",
            return_value=(agent, [], ""),
        ),
    ):
        connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        connect.return_value.__exit__ = MagicMock(return_value=False)
        main_single_query("/repo", 100, QUESTION, output_format=output_format)


def _registry(agent: Agent[None, str | DeferredToolRequests]) -> MCPToolsRegistry:
    handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
    handler._ingestor_lock = asyncio.Lock()
    handler.ingestor = MagicMock()
    handler.ingestor.fetch_all = MagicMock(return_value=[])
    handler.project_root = "/repo"
    handler._graph_incomplete = False
    handler._incomplete_project = None
    handler._flag_from_failed_clear = None
    handler._persisted_incomplete = MagicMock(return_value=False)
    handler.rag_agent = agent
    return handler


def test_ask_agent_with_no_confirm_applies_the_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], confirm_edits: None
) -> None:
    app_context.session.confirm_edits = False

    _single_query(_agent(tmp_path))

    assert capsys.readouterr().out.strip() == "Done: wrote NOTES.md"
    assert (tmp_path / "NOTES.md").read_text(encoding="utf-8") == "hello\n"


def test_ask_agent_without_no_confirm_denies_the_edit_and_still_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], confirm_edits: None
) -> None:
    app_context.session.confirm_edits = True

    _single_query(_agent(tmp_path))

    assert capsys.readouterr().out.strip() == (f"Done: {cs.ASK_AGENT_APPROVAL_DENIED}")
    assert not (tmp_path / "NOTES.md").exists()


def test_ask_agent_json_never_carries_the_repr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], confirm_edits: None
) -> None:
    app_context.session.confirm_edits = True

    _single_query(_agent(tmp_path), cs.QueryFormat.JSON)

    payload = json.loads(capsys.readouterr().out)
    assert payload[cs.KEY_RESPONSE] == f"Done: {cs.ASK_AGENT_APPROVAL_DENIED}"


def test_a_model_that_never_stops_asking_ends_in_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], confirm_edits: None
) -> None:
    app_context.session.confirm_edits = True

    with pytest.raises(RuntimeError, match="approval"):
        _single_query(_agent(tmp_path, insist=True))

    assert REPR not in capsys.readouterr().out


@pytest.mark.anyio
async def test_mcp_ask_agent_denies_the_edit_and_answers(tmp_path: Path) -> None:
    result = await _registry(_agent(tmp_path)).ask_agent(QUESTION)

    assert result == {"output": f"Done: {cs.MCP_ASK_AGENT_APPROVAL_DENIED}"}
    assert not (tmp_path / "NOTES.md").exists()


@pytest.mark.anyio
async def test_mcp_ask_agent_reports_a_model_that_never_stops_asking(
    tmp_path: Path,
) -> None:
    result = await _registry(_agent(tmp_path, insist=True)).ask_agent(QUESTION)

    assert set(result) == {cs.DICT_KEY_ERROR}
    assert REPR not in result[cs.DICT_KEY_ERROR]


# Negative: a run that answers in text is unchanged.


def test_ask_agent_text_answer_is_printed_as_before(
    capsys: pytest.CaptureFixture[str], confirm_edits: None
) -> None:
    app_context.session.confirm_edits = True

    _single_query(_text_agent())

    assert capsys.readouterr().out.strip() == "The parser builds a graph."


@pytest.mark.anyio
async def test_mcp_ask_agent_text_answer_is_returned_as_before() -> None:
    result = await _registry(_text_agent()).ask_agent("what does the parser do?")

    assert result == {"output": "The parser builds a graph."}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
