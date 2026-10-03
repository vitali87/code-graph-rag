"""Issue #2359: an eval of which tool the orchestrator reaches for first.

For a structural question (callers, inheritance, counts, package layout,
dependencies) the first tool call should be a graph query, not the shell.
The eval runs the orchestrator's own prompt and tool definitions and stops at
the first call, so no tool runs and nothing asks for approval.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from codebase_rag.services import ReadOnlyQueryProtocol
from codebase_rag.tools.shell_command import ShellCommander, create_shell_command_tool
from codebase_rag.tools.tool_descriptions import AgenticToolName
from evals import first_tool as ft


def _query_graph(natural_language_query: str) -> str:
    """Query the codebase knowledge graph."""
    raise AssertionError("the eval must not run a tool")


def _orchestrator_answering_with(
    project: Path,
    first_call: ToolCallPart | None,
    seen: list[set[str]] | None = None,
) -> Agent[None, str]:
    def reply_with_first_call(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        if seen is not None:
            seen.append({tool.name for tool in info.function_tools})
        if first_call is None:
            return ModelResponse(parts=[TextPart("answered from memory")])
        return ModelResponse(parts=[first_call])

    return Agent(
        FunctionModel(reply_with_first_call),
        system_prompt="orchestrator",
        tools=[
            Tool(_query_graph, name=AgenticToolName.QUERY_GRAPH),
            create_shell_command_tool(ShellCommander(str(project), timeout=10)),
        ],
    )


async def test_a_graph_query_first_is_graded_graph_first(tmp_path: Path) -> None:
    agent = _orchestrator_answering_with(
        tmp_path,
        ToolCallPart(
            AgenticToolName.QUERY_GRAPH,
            {"natural_language_query": "what calls GraphUpdater.run"},
        ),
    )

    call = await ft.first_tool_call(agent, "What calls GraphUpdater.run?")

    assert call == AgenticToolName.QUERY_GRAPH
    assert ft.is_graph_first(call)


async def test_a_shell_command_first_is_a_miss_and_never_runs(tmp_path: Path) -> None:
    # Negative: the shell is graded a miss, and the probe stops before the
    # command runs or asks for approval.
    agent = _orchestrator_answering_with(
        tmp_path,
        ToolCallPart(AgenticToolName.EXECUTE_SHELL, {"command": "touch ran.txt"}),
    )

    call = await ft.first_tool_call(agent, "How big is this codebase?")

    assert call == AgenticToolName.EXECUTE_SHELL
    assert not ft.is_graph_first(call)
    assert not (tmp_path / "ran.txt").exists()


async def test_an_answer_without_a_tool_is_a_miss(tmp_path: Path) -> None:
    # Negative: answering from memory is not answering from the graph.
    call = await ft.first_tool_call(
        _orchestrator_answering_with(tmp_path, None), "Which classes inherit?"
    )

    assert call is None
    assert not ft.is_graph_first(call)


async def test_the_model_sees_every_tool_and_the_agent_keeps_them(
    tmp_path: Path,
) -> None:
    seen: list[set[str]] = []
    agent = _orchestrator_answering_with(
        tmp_path,
        ToolCallPart(AgenticToolName.QUERY_GRAPH, {"natural_language_query": "q"}),
        seen,
    )

    await ft.first_tool_call(agent, "q")
    await ft.first_tool_call(agent, "q")

    expected = {AgenticToolName.QUERY_GRAPH, AgenticToolName.EXECUTE_SHELL}
    assert seen == [expected, expected]


def test_the_questions_are_the_structural_kinds_the_issue_lists() -> None:
    text = " ".join(ft.STRUCTURAL_QUESTIONS).lower()
    for kind in ("most-called", "calls", "inherit", "how big", "depend", "packages"):
        assert kind in text, kind


def test_the_summary_counts_the_graph_first_answers() -> None:
    summary = ft.count_graph_first(
        [
            ft.FirstToolRecord(question="a", tool=AgenticToolName.QUERY_GRAPH),
            ft.FirstToolRecord(question="b", tool=AgenticToolName.EXECUTE_SHELL),
            ft.FirstToolRecord(question="c", tool=None),
        ]
    )

    assert (summary.graph_first, summary.total) == (1, 3)


_Built = tuple[str, list[str] | None]


def _patch_orchestrator(
    monkeypatch: pytest.MonkeyPatch, agent: Agent[None, str], calls: list[_Built]
) -> None:
    from codebase_rag import cli_runtime
    from codebase_rag import main as cgr_main

    def fake_initialize(
        repo_path: str,
        ingestor: ReadOnlyQueryProtocol,
        active_projects: list[str] | None = None,
    ) -> tuple[Agent[None, str], None, str]:
        calls.append((repo_path, active_projects))
        return agent, None, ""

    monkeypatch.setattr(cgr_main, "_initialize_services_and_agent", fake_initialize)
    monkeypatch.setattr(
        cli_runtime, "connect_memgraph", lambda batch_size: nullcontext(MagicMock())
    )


def test_the_eval_passes_when_every_question_goes_to_the_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[_Built] = []
    agent = _orchestrator_answering_with(
        tmp_path,
        ToolCallPart(AgenticToolName.QUERY_GRAPH, {"natural_language_query": "q"}),
    )
    _patch_orchestrator(monkeypatch, agent, calls)

    ft.main(repo_path=tmp_path, project_name="proj")

    # The orchestrator is built as `cgr start` builds it, for this repo.
    assert calls == [(str(tmp_path.resolve()), ["proj"])]


def test_the_eval_fails_when_a_question_goes_to_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: one shell-first answer fails the run.
    agent = _orchestrator_answering_with(
        tmp_path,
        ToolCallPart(AgenticToolName.EXECUTE_SHELL, {"command": "ls"}),
    )
    _patch_orchestrator(monkeypatch, agent, [])

    with pytest.raises(SystemExit) as exit_info:
        ft.main(repo_path=tmp_path, project_name="")

    assert exit_info.value.code == 1
