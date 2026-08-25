# Agentic tool wrapper for ast-grep structural search (#415).
from __future__ import annotations

import asyncio

from pydantic_ai import Tool

from .. import constants as cs
from ..taint import ReadContentRecord
from ..types_defs import StructuralSearchMatch
from ..utils.dependencies import has_ast_grep
from . import tool_descriptions as td
from .ast_grep_service import AstGrepService


def format_matches(matches: list[StructuralSearchMatch]) -> str:
    """Render matches as `file:line:column  text`, flagging a truncated
    result list so the agent does not read a capped list as complete."""
    lines = [f"{m['file']}:{m['line']}:{m['column']}  {m['text']}" for m in matches]
    # make the result cap visible to the caller: without this the agent sees
    # a truncated list and assumes it is complete.
    if len(matches) >= cs.AST_GREP_MAX_RESULTS:
        lines.append(cs.AST_GREP_TRUNCATED.format(limit=cs.AST_GREP_MAX_RESULTS))
    return "\n".join(lines)


def create_structural_search_tool(
    service: AstGrepService, read_record: ReadContentRecord | None = None
) -> Tool:
    """Build the `structural_search` tool, recording matched source in
    `read_record` so it feeds the egress taint gate (issue #1128)."""

    async def structural_search(pattern: str, language: str | None = None) -> str:
        if not has_ast_grep():
            return cs.AST_GREP_NOT_AVAILABLE
        try:
            # offload to a thread: search does blocking os.walk + file reads
            # and CPU-bound AST parsing, which would stall the event loop.
            matches = await asyncio.to_thread(
                service.search, pattern, language=language
            )
        # catch broadly: ast-grep-py's Rust bindings raise beyond ValueError
        # (RuntimeError and others); report it rather than crash the turn.
        except Exception as e:
            return str(e)
        if not matches:
            return cs.AST_GREP_NO_MATCHES.format(pattern=pattern)
        formatted = format_matches(matches)
        if read_record is not None:
            # Matched lines are repository source; feed the egress taint gate
            # (issue #1128).
            read_record.record(formatted)
        return formatted

    return Tool(
        function=structural_search,
        name=td.AgenticToolName.STRUCTURAL_SEARCH,
        description=td.STRUCTURAL_SEARCH,
    )
