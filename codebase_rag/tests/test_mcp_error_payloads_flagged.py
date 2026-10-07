"""Every failed MCP tool call reaches the client with `isError: true`.

After #2650 the server flagged a returned `ToolFailure` and a JSON result that
is nothing but `{"error": ...}`. A refusal that carries its error beside
other fields still went out as a success: a `reingest` rejected for a path
outside the repository (`{"error", "reparsed": [], ...}`), a refused
`rename` (`{"error", "ambiguous", "unlocatable"}`), `delete_project` of an
unknown project (`{"success": false, "error"}`), and the text tools
`structural_search` (unknown language) and `find_duplicate_code` (a bad
threshold) (issues #2802 and #2785).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp import server as mcp_server
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tests.test_mcp_tool_errors_flagged import _call, _text

pytestmark = [pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lib.py").write_text("def helper(a):\n    return a\n", encoding="utf-8")
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(root))
    return root


@pytest.fixture
def no_llm() -> Iterator[None]:
    with patch.object(mcp_server, "LazyCypherGenerator", MagicMock()):
        yield


_ERROR = "Path is outside the repository: ../outside.py"


@pytest.mark.parametrize(
    ("method", "tool", "arguments", "payload"),
    [
        pytest.param(
            "reingest",
            cs.MCPToolName.REINGEST,
            {"paths": ["../outside.py"]},
            {"error": _ERROR, "reparsed": [], "affected": [], "removed": []},
            id="reingest-refused",
        ),
        pytest.param(
            "rename",
            cs.MCPToolName.RENAME,
            {"qualified_name": "no.such.fn", "new_name": "z"},
            {"error": _ERROR, "ambiguous": [], "unlocatable": []},
            id="rename-refused",
        ),
        pytest.param(
            "delete_project",
            cs.MCPToolName.DELETE_PROJECT,
            {"project_name": "nope"},
            {"success": False, "error": _ERROR},
            id="delete-unknown-project",
        ),
    ],
)
async def test_an_error_beside_other_fields_is_flagged(
    repo: Path,
    no_llm: None,
    method: str,
    tool: str,
    arguments: dict[str, object],
    payload: dict[str, object],
) -> None:
    with patch.object(MCPToolsRegistry, method, AsyncMock(return_value=payload)):
        result = await _call(tool, arguments)
    assert result.isError is True, _text(result)
    assert _ERROR in _text(result)


@pytest.mark.parametrize(
    ("tool", "arguments", "named"),
    [
        pytest.param(
            cs.MCPToolName.STRUCTURAL_SEARCH,
            {"pattern": "x($A)", "language": "cobol"},
            "cobol",
            id="structural-search-unknown-language",
        ),
        pytest.param(
            cs.MCPToolName.FIND_DUPLICATE_CODE,
            {"threshold": 5},
            "threshold",
            id="duplicates-bad-threshold",
        ),
        pytest.param(
            cs.MCPToolName.FIND_DUPLICATE_CODE,
            {"min_size": 0},
            "min_size",
            id="duplicates-bad-min-size",
        ),
    ],
)
async def test_a_text_tool_refusal_is_flagged(
    repo: Path, no_llm: None, tool: str, arguments: dict[str, object], named: str
) -> None:
    pytest.importorskip("ast_grep_py")
    result = await _call(tool, arguments)
    assert result.isError is True, _text(result)
    assert named in _text(result)


@pytest.mark.parametrize(
    ("method", "tool", "arguments", "payload"),
    [
        # Negatives: work that was done, an empty answer, and a result whose
        # error field is empty are successes.
        pytest.param(
            "rename",
            cs.MCPToolName.RENAME,
            {"qualified_name": "a.b", "new_name": "c"},
            {"applied": True, "error": "could not record the marker", "sites": []},
            id="applied-rename-with-marker-error",
        ),
        pytest.param(
            "delete_project",
            cs.MCPToolName.DELETE_PROJECT,
            {"project_name": "p"},
            {"success": True, "project": "p"},
            id="deleted",
        ),
        pytest.param(
            "definition",
            cs.MCPToolName.DEFINITION,
            {"qualified_name": "a.b"},
            {"found": False, "qualified_name": "a.b"},
            id="definition-not-found",
        ),
        pytest.param(
            "reingest",
            cs.MCPToolName.REINGEST,
            {"paths": ["lib.py"]},
            {"error": None, "reparsed": ["lib.py"]},
            id="reingest-ok-with-empty-error",
        ),
    ],
)
async def test_a_success_is_not_flagged(
    repo: Path,
    no_llm: None,
    method: str,
    tool: str,
    arguments: dict[str, object],
    payload: dict[str, object],
) -> None:
    with patch.object(MCPToolsRegistry, method, AsyncMock(return_value=payload)):
        result = await _call(tool, arguments)
    assert result.isError is False, _text(result)
