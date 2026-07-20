# (H) Agentic tool wrapper for ast-grep structural search (#415).
from __future__ import annotations

from pydantic_ai import Tool

from .. import constants as cs
from ..types_defs import StructuralSearchMatch
from ..utils.dependencies import has_ast_grep
from . import tool_descriptions as td
from .ast_grep_service import AstGrepService


def format_matches(matches: list[StructuralSearchMatch]) -> str:
    return "\n".join(
        f"{m['file']}:{m['line']}:{m['column']}  {m['text']}" for m in matches
    )


def create_structural_search_tool(service: AstGrepService) -> Tool:
    async def structural_search(pattern: str, language: str | None = None) -> str:
        if not has_ast_grep():
            return cs.AST_GREP_NOT_AVAILABLE
        try:
            matches = service.search(pattern, language=language)
        except ValueError as e:
            return str(e)
        if not matches:
            return cs.AST_GREP_NO_MATCHES.format(pattern=pattern)
        return format_matches(matches)

    return Tool(
        function=structural_search,
        name=td.AgenticToolName.STRUCTURAL_SEARCH,
        description=td.STRUCTURAL_SEARCH,
    )
