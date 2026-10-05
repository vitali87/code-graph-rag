# First-tool eval (issue #2359): for a structural question the interactive
# orchestrator should reach for the graph before the shell. Recorded agent
# sessions showed it answering "what calls X?", "how big is this codebase?" and
# "which classes inherit from Y?" by grepping and listing directories, every
# command behind an approval prompt, while one graph query held the answer.
#
# The eval builds the orchestrator exactly as `cgr start` does (same system
# prompt, same tool names, descriptions and schemas, same configured model)
# and stops each run at its FIRST tool call, before the tool runs. Nothing is
# executed, nothing asks for approval, and a run costs one model request per
# question. A question passes when that first call is a graph tool.
import asyncio
import sys
from pathlib import Path
from typing import Annotated, NamedTuple

import typer
from pydantic_ai import Agent, RunContext
from pydantic_ai.toolsets import FunctionToolset, ToolsetTool, WrapperToolset
from rich.console import Console
from rich.table import Table

from codebase_rag.tools.tool_descriptions import AgenticToolName
from codebase_rag.types_defs import JsonValue

from . import constants as ec
from . import logs as ls

console = Console()

# The issue's recorded questions, one per structural kind: counts, callers,
# where things live, size, dependencies, package layout, inheritance.
STRUCTURAL_QUESTIONS: tuple[str, ...] = (
    "What are the five most-called functions in this repo?",
    "What calls GraphUpdater.run?",
    "Which LLM providers does it support, and where is each one implemented?",
    "How big is this codebase? Give me some numbers.",
    "Which external libraries does it depend on?",
    "What are the main packages and what does each one do?",
    "Which classes inherit from ModelProvider?",
)

# Tools that answer from the graph. Semantic search counts: it ranks graph
# nodes, and "where is X implemented" is a fair question for it.
GRAPH_TOOLS = frozenset({AgenticToolName.QUERY_GRAPH, AgenticToolName.SEMANTIC_SEARCH})


class FirstToolRecord(NamedTuple):
    question: str
    tool: str | None


class FirstToolSummary(NamedTuple):
    graph_first: int
    total: int


class _FirstToolCall(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class _StopAtFirstCall(WrapperToolset[None]):
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, JsonValue],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> JsonValue:
        raise _FirstToolCall(name)


def is_graph_first(tool: str | None) -> bool:
    return tool in GRAPH_TOOLS


async def first_tool_call[OutputT](
    agent: Agent[None, OutputT], question: str
) -> str | None:
    """The name of the first tool the agent calls for `question`, or None when
    it answers without one. The tool itself never runs."""
    tools = [
        tool
        for toolset in agent.toolsets
        if isinstance(toolset, FunctionToolset)
        for tool in toolset.tools.values()
    ]
    with agent.override(tools=[], toolsets=[_StopAtFirstCall(FunctionToolset(tools))]):
        try:
            await agent.run(question)
        except _FirstToolCall as call:
            return call.name
    return None


def count_graph_first(records: list[FirstToolRecord]) -> FirstToolSummary:
    return FirstToolSummary(
        graph_first=sum(is_graph_first(record.tool) for record in records),
        total=len(records),
    )


async def _record_first_tool_calls[OutputT](
    agent: Agent[None, OutputT],
) -> list[FirstToolRecord]:
    records: list[FirstToolRecord] = []
    for question in STRUCTURAL_QUESTIONS:
        records.append(
            FirstToolRecord(question, await first_tool_call(agent, question))
        )
    return records


def _print_first_tool_report(records: list[FirstToolRecord]) -> None:
    table = Table(title=ec.FIRST_TOOL_TABLE_TITLE)
    table.add_column(ec.FIRST_TOOL_COL_QUESTION)
    table.add_column(ec.FIRST_TOOL_COL_TOOL)
    table.add_column(ec.FIRST_TOOL_COL_GRAPH_FIRST)
    for record in records:
        table.add_row(
            record.question,
            record.tool or ec.FIRST_TOOL_NO_TOOL,
            ec.FIRST_TOOL_PASS if is_graph_first(record.tool) else ec.FIRST_TOOL_MISS,
        )
    console.print(table)
    summary = count_graph_first(records)
    console.print(
        ls.FIRST_TOOL_SUMMARY.format(
            graph_first=summary.graph_first, total=summary.total
        )
    )


def main(
    repo_path: Annotated[
        Path, typer.Option(help="Indexed repository the questions are about.")
    ] = Path("."),
    project_name: Annotated[
        str, typer.Option(help="Indexed project to scope the graph to.")
    ] = "",
) -> None:
    from codebase_rag.cli_runtime import connect_memgraph
    from codebase_rag.config import settings
    from codebase_rag.main import _initialize_services_and_agent

    root = str(repo_path.resolve())
    projects = [project_name] if project_name else None
    with connect_memgraph(settings.MEMGRAPH_BATCH_SIZE) as ingestor:
        agent, _, _ = _initialize_services_and_agent(root, ingestor, projects)
        records = asyncio.run(_record_first_tool_calls(agent))
    _print_first_tool_report(records)
    summary = count_graph_first(records)
    if summary.graph_first < summary.total:
        sys.exit(1)


if __name__ == "__main__":
    typer.run(main)
