# (H) Agentic tool wrapper for ast-grep structural search (#415).
from __future__ import annotations

import asyncio

from pydantic_ai import Tool

from .. import constants as cs
from ..types_defs import StructuralSearchMatch
from ..utils.dependencies import has_ast_grep
from . import tool_descriptions as td
from .ast_grep_service import AstGrepService


def format_matches(matches: list[StructuralSearchMatch]) -> str:
    lines = [f"{m['file']}:{m['line']}:{m['column']}  {m['text']}" for m in matches]
    # (H) make the result cap visible to the caller: without this the agent sees
    # (H) a truncated list and assumes it is complete.
    if len(matches) >= cs.AST_GREP_MAX_RESULTS:
        lines.append(cs.AST_GREP_TRUNCATED.format(limit=cs.AST_GREP_MAX_RESULTS))
    return "\n".join(lines)


def create_structural_search_tool(service: AstGrepService) -> Tool:
    async def structural_search(pattern: str, language: str | None = None) -> str:
        if not has_ast_grep():
            return cs.AST_GREP_NOT_AVAILABLE
        try:
            # (H) offload to a thread: search does blocking os.walk + file reads
            # (H) and CPU-bound AST parsing, which would stall the event loop.
            matches = await asyncio.to_thread(
                service.search, pattern, language=language
            )
        # (H) catch broadly: ast-grep-py's Rust bindings raise beyond ValueError
        # (H) (RuntimeError and others); report it rather than crash the turn.
        except Exception as e:
            return str(e)
        if not matches:
            return cs.AST_GREP_NO_MATCHES.format(pattern=pattern)
        return format_matches(matches)

    return Tool(
        function=structural_search,
        name=td.AgenticToolName.STRUCTURAL_SEARCH,
        description=td.STRUCTURAL_SEARCH,
    )
