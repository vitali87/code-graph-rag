# Tests for the structural search/replace tool wrappers and their MCP
# registration (#415): guard/error/no-match branches the service tests do
# not reach, plus end-to-end MCP handler delegation.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from codebase_rag.mcp.tools import MCPToolsRegistry

pytest.importorskip("ast_grep_py")

from codebase_rag import constants as cs
from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.tools.structural_editor import create_structural_editor_tool
from codebase_rag.tools.structural_search import create_structural_search_tool


async def test_search_tool_formats_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print(x)\n")
    tool = create_structural_search_tool(AstGrepService(str(tmp_path)))
    out = await tool.function(pattern="print($A)")
    assert "a.py:1:0" in out
    assert "print(x)" in out


async def test_search_tool_reports_truncation(tmp_path: Path) -> None:
    body = "".join(f"print({i})\n" for i in range(cs.AST_GREP_MAX_RESULTS + 5))
    (tmp_path / "a.py").write_text(body)
    tool = create_structural_search_tool(AstGrepService(str(tmp_path)))
    out = await tool.function(pattern="print($A)")
    assert cs.AST_GREP_TRUNCATED.format(limit=cs.AST_GREP_MAX_RESULTS) in out


async def test_search_tool_no_match_message(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    tool = create_structural_search_tool(AstGrepService(str(tmp_path)))
    out = await tool.function(pattern="print($A)")
    assert out == cs.AST_GREP_NO_MATCHES.format(pattern="print($A)")


async def test_search_tool_unknown_language_message(tmp_path: Path) -> None:
    tool = create_structural_search_tool(AstGrepService(str(tmp_path)))
    out = await tool.function(pattern="print($A)", language="cobol")
    assert "cobol" in out


async def test_search_tool_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codebase_rag.tools.structural_search.has_ast_grep", lambda: False
    )
    tool = create_structural_search_tool(AstGrepService(str(tmp_path)))
    out = await tool.function(pattern="print($A)")
    assert out == cs.AST_GREP_NOT_AVAILABLE


async def test_editor_tool_dry_run_then_apply(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("print(x)\n")
    tool = create_structural_editor_tool(AstGrepService(str(tmp_path)))
    dry = await tool.function(pattern="print($A)", rewrite="log($A)", dry_run=True)
    assert "Dry run" in dry
    assert target.read_text() == "print(x)\n"
    await tool.function(pattern="print($A)", rewrite="log($A)", dry_run=False)
    assert "log(x)" in target.read_text()


async def test_editor_tool_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codebase_rag.tools.structural_editor.has_ast_grep", lambda: False
    )
    tool = create_structural_editor_tool(AstGrepService(str(tmp_path)))
    out = await tool.function(pattern="print($A)", rewrite="log($A)")
    assert out == cs.AST_GREP_NOT_AVAILABLE


def _registry(root: Path) -> MCPToolsRegistry:
    from codebase_rag.mcp.tools import MCPToolsRegistry

    return MCPToolsRegistry(str(root), MagicMock(), MagicMock())


def test_mcp_registers_structural_tools(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    names = {schema.name for schema in reg.get_tool_schemas()}
    assert cs.MCPToolName.STRUCTURAL_SEARCH in names
    assert cs.MCPToolName.STRUCTURAL_REPLACE in names


async def test_mcp_structural_search_handler(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print(x)\n")
    reg = _registry(tmp_path)
    out = await reg.structural_search("print($A)")
    assert "a.py:1:0" in out


async def test_mcp_structural_replace_handler_apply(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("print(x)\n")
    reg = _registry(tmp_path)
    await reg.structural_replace("print($A)", "log($A)", dry_run=False)
    assert "log(x)" in target.read_text()
