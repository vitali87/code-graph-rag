# Agentic tool wrapper for ast-grep structural replace (#415). Rewrites are
# gated twice: dry_run defaults to a preview, and the tool requires approval
# before any invocation actually touches disk.
from __future__ import annotations

import asyncio

from pydantic_ai import Tool

from .. import constants as cs
from ..types_defs import StructuralReplaceChange
from ..utils.dependencies import has_ast_grep
from . import tool_descriptions as td
from .ast_grep_service import AstGrepService


def format_changes(changes: list[StructuralReplaceChange], dry_run: bool) -> str:
    header = (
        cs.AST_GREP_DRY_RUN_HEADER if dry_run else cs.AST_GREP_APPLIED_HEADER
    ).format(count=len(changes))
    bodies = [f"{c['file']} ({c['matches']} match(es))\n{c['diff']}" for c in changes]
    return "\n\n".join([header, *bodies])


def create_structural_editor_tool(service: AstGrepService) -> Tool:
    async def structural_replace(
        pattern: str,
        rewrite: str,
        language: str | None = None,
        dry_run: bool = True,
    ) -> str:
        if not has_ast_grep():
            return cs.AST_GREP_NOT_AVAILABLE
        try:
            # offload to a thread: replace does blocking os.walk, file reads
            # and writes, and CPU-bound AST parsing, which would stall the
            # event loop.
            changes = await asyncio.to_thread(
                service.replace,
                pattern,
                rewrite,
                language=language,
                dry_run=dry_run,
            )
        # catch broadly: ast-grep-py's Rust bindings raise beyond ValueError
        # (RuntimeError and others); report it rather than crash the turn.
        except Exception as e:
            return str(e)
        if not changes:
            return cs.AST_GREP_NO_MATCHES.format(pattern=pattern)
        return format_changes(changes, dry_run)

    return Tool(
        function=structural_replace,
        name=td.AgenticToolName.STRUCTURAL_REPLACE,
        description=td.STRUCTURAL_EDITOR,
        requires_approval=True,
    )
