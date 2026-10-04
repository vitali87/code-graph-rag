"""Issue #2678: the MCP docs describe the shared graph as it is.

Under the multi-repository example, the guide warned that indexing a new
repository clears the previous repository's data, and the Claude Code setup
page said the same. `index_repository` deletes and rebuilds only the server's
own project, and the graph is shared by design, so the warning told readers
that the setup the guide had just shown would make each instance wipe the
other.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.tools import tool_descriptions as td

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "guide" / "mcp-server.md"
SETUP = REPO / "docs" / "claude-code-setup.md"


def _multi_repository_section() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    start = text.index("## Multi-Repository Setup")
    end = text.index("\n### ", start)
    return text[start:end]


def test_the_guide_no_longer_says_indexing_clears_another_repository() -> None:
    section = _multi_repository_section()

    assert "automatically cleared" not in section
    assert "Only one repository can be indexed" not in section


def test_the_guide_says_what_each_tool_touches() -> None:
    section = _multi_repository_section()

    for tool in (
        cs.MCPToolName.INDEX_REPOSITORY,
        cs.MCPToolName.UPDATE_REPOSITORY,
        cs.MCPToolName.WIPE_DATABASE,
    ):
        assert f"`{tool}`" in section
    assert "`TARGET_REPO_PATH`" in section
    assert "not touched" in section


def test_the_claude_code_setup_page_makes_no_such_claim() -> None:
    text = SETUP.read_text(encoding="utf-8")

    assert "automatically cleared" not in text
    assert "clears previous repository data" not in text
    assert "Only one repository can be indexed" not in text


# Negative: the tool descriptions the guide agrees with are unchanged.


def test_index_repository_still_names_only_the_current_project() -> None:
    assert "current project" in td.MCP_INDEX_REPOSITORY


def test_wipe_database_still_names_every_project() -> None:
    assert "ALL indexed projects" in td.MCP_WIPE_DATABASE
