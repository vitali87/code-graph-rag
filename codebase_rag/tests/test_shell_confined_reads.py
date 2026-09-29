"""Issue #2359: confined shell reads run without a prompt; refusals never prompt.

Every `ls`, `rg`, `head` or `wc` the agent ran in interactive mode asked for
approval, although the same reads are allowed unattended in non-interactive
runs and the file reader tool reads any project file without asking. And a
command the allowlist rejects (`grep`) was put to the user first, then
refused whatever they answered.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai import ApprovalRequired

from codebase_rag import prompts
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.shell_command import ShellCommander, create_shell_command_tool


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def run():\n    return 1\n")
    (root / "README.md").write_text("# demo\nline two\n")
    return root


def _tool(project: Path):
    return create_shell_command_tool(ShellCommander(str(project), timeout=10))


def _unapproved() -> MagicMock:
    ctx = MagicMock()
    ctx.tool_call_approved = False
    return ctx


@pytest.mark.parametrize(
    "command",
    [
        "ls pkg",
        "rg -n run pkg",
        "head -1 README.md",
        "wc -l README.md",
        "cat pkg/mod.py | wc -l",
        "find pkg -name '*.py'",
    ],
)
async def test_a_confined_read_runs_without_a_prompt(
    project: Path, command: str
) -> None:
    result = await _tool(project).function(_unapproved(), command)

    assert result.return_code == 0, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/hostname",
        "ls ..",
        "rg -L run .",
        "sort -o out.txt README.md",
        "cat README.md > copy.md",
        "find . -delete",
        "rm README.md",
        "git log -1",
    ],
)
async def test_anything_else_still_asks(project: Path, command: str) -> None:
    # Negative: absolute paths, traversal, symlink following, write forms,
    # redirects, mutating find, writes and git keep the prompt.
    with pytest.raises(ApprovalRequired):
        await _tool(project).function(_unapproved(), command)


async def test_a_command_the_allowlist_rejects_is_refused_without_a_prompt(
    project: Path,
) -> None:
    result = await _tool(project).function(_unapproved(), "grep -rn run pkg")

    assert result.return_code != 0
    assert "rg" in result.stderr


async def test_a_refused_command_is_refused_even_when_approved(project: Path) -> None:
    # Negative: approval never widens the allowlist.
    ctx = MagicMock()
    ctx.tool_call_approved = True

    result = await _tool(project).function(ctx, "curl http://example.com")

    assert result.return_code != 0
    assert "allowlist" in result.stderr


def test_the_shell_description_sends_structure_to_the_graph() -> None:
    description = td.SHELL_COMMAND
    assert td.AgenticToolName.QUERY_GRAPH in description
    assert "grep" in description and "rg" in description


def test_the_prompt_routes_structural_questions_to_the_graph() -> None:
    prompt = prompts.build_rag_orchestrator_prompt([])
    assert "fallback" in prompt.lower()
    for kind in ("callers", "inherit", "most-called", "layout", "dependencies"):
        assert kind in prompt.lower(), kind
