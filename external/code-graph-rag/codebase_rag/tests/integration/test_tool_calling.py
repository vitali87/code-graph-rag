from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from loguru import logger
from pydantic_ai import Tool

from codebase_rag.services.llm import create_rag_orchestrator

pytestmark = [pytest.mark.asyncio(loop_scope="module"), pytest.mark.integration]

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage

logger.remove()
logger.add(sys.stderr, level="INFO")


class ToolCallTracker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def clear(self) -> None:
        self.calls.clear()

    def log_call(self, tool_name: str) -> None:
        self.calls.append(tool_name)
        logger.info(f"Tool called: {tool_name}")


def create_tracking_tools(tracker: ToolCallTracker) -> list[Tool]:
    async def semantic_search(query: str) -> str:
        tracker.log_call("semantic_search")
        return f"Semantic search results for: {query}"

    async def read_file(file_path: str) -> str:
        tracker.log_call("read_file")
        return f"Contents of {file_path}: def main(): pass"

    async def query_graph(query: str) -> str:
        tracker.log_call("query_graph")
        return f"Graph results for: {query}"

    async def list_directory(path: str) -> str:
        tracker.log_call("list_directory")
        return f"Files in {path}: main.py, config.py, utils.py"

    return [
        Tool(semantic_search, name="semantic_search"),
        Tool(read_file, name="read_file"),
        Tool(query_graph, name="query_graph"),
        Tool(list_directory, name="list_directory"),
    ]


def find_skipped_tools(messages: list[ModelMessage]) -> list[str]:
    skipped = []
    for msg in messages:
        msg_str = str(msg)
        if "Tool not executed" in msg_str:
            skipped.append(msg_str)
        if "Output tool not used" in msg_str:
            skipped.append(msg_str)
    return skipped


def log_message_history(messages: list[ModelMessage], label: str) -> None:
    logger.info(f"\n{'=' * 60}\n{label} - Message History:\n{'=' * 60}")
    for i, msg in enumerate(messages):
        logger.info(f"[{i}] {type(msg).__name__}: {msg}")


async def run_agent_test(
    agent: Agent, prompt: str, tracker: ToolCallTracker, label: str
) -> tuple[list[str], list[str]]:
    tracker.clear()
    logger.info(f"\n{'#' * 60}\nRunning: {label}\nPrompt: {prompt}\n{'#' * 60}")

    result = await agent.run(prompt)

    messages = result.all_messages()
    log_message_history(messages, label)

    calls = tracker.calls.copy()
    skipped = find_skipped_tools(messages)

    logger.info(f"\n{label} Summary:")
    logger.info(f"  Tools called: {calls}")
    logger.info(f"  Skipped tools: {skipped}")
    logger.info(
        f"  Output: {result.output[:200] if isinstance(result.output, str) else result.output}"
    )

    return calls, skipped


@pytest.fixture(scope="module")
def tracker() -> ToolCallTracker:
    return ToolCallTracker()


@pytest.fixture(scope="module")
def tracking_tools(tracker: ToolCallTracker) -> list[Tool]:
    return create_tracking_tools(tracker)


@pytest.fixture(scope="module")
def agent(tracking_tools: list[Tool]) -> Agent:
    try:
        return create_rag_orchestrator(tracking_tools)
    except Exception as e:
        pytest.skip(f"Ollama server not available: {e}")


PARALLEL_PROMPT = """Execute ALL of these tasks in parallel, not sequentially:
1. Use semantic_search to find "main entry point"
2. Use read_file to read "main.py"
3. Use query_graph to find "all functions"
4. Use list_directory to list "."

Call all 4 tools simultaneously in your response."""

HYBRID_PROMPT = """Find the main entry point and show its code:
1. First search semantically for "entry point main"
2. Read the main.py file
3. Query the graph for function relationships

Use all available tools to answer comprehensively."""


class TestToolCallingIntegration:
    async def test_parallel_tool_calls_all_execute(
        self, agent: Agent, tracker: ToolCallTracker
    ) -> None:
        calls, skipped = await run_agent_test(
            agent, PARALLEL_PROMPT, tracker, "Parallel tool calls"
        )

        assert len(skipped) == 0, f"Tools were unexpectedly skipped: {skipped}"
        assert len(calls) >= 4, f"Expected at least 4 tool calls, got {len(calls)}"

        logger.info(f"Tools called: {len(calls)}, Skipped: {len(skipped)}")

    async def test_hybrid_search_completes(
        self, agent: Agent, tracker: ToolCallTracker
    ) -> None:
        calls, skipped = await run_agent_test(
            agent, HYBRID_PROMPT, tracker, "Hybrid search"
        )

        assert len(skipped) == 0, f"Tools were unexpectedly skipped: {skipped}"
        assert len(calls) >= 1, f"Expected at least 1 tool call, got {len(calls)}"

        logger.info(f"Tools called: {len(calls)}, Skipped: {len(skipped)}")
